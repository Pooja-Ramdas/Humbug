# KSPDB Fault Localization — System Overview

This is a reference for whoever (human or AI tool) needs to debug or extend
this system without the full conversation history that produced it. Paste
this whole file as context.

## What this system does

Detects and localizes power faults on a synthetic LT distribution network
from noisy, incomplete pole-telemetry, and surfaces them on a map-based
operator console with a fault simulator for testing.

## Directory layout

```
kspdb-sim/
├── generate_data.py       # one-time: generates the synthetic pole/DT/feeder
│                           # registries + writes data.db. Run once, or whenever
│                           # you want a fresh dataset with a different seed.
├── build_graph.py          # standalone CLI for inspecting the graph. NOT
│                           # required at runtime -- graph_lib.py (a copy) is
│                           # imported directly by the backend.
├── backend/
│   ├── data.db              # SQLite. Single source of truth. See schema below.
│   ├── graph_lib.py          # copy of build_graph.py's build_graph() function
│   ├── telemetry_sim.py     # turns "fault at X" into realistic degraded events
│   ├── detection.py         # turns telemetry into pole status + tickets
│   └── server.py            # Flask API, ties it all together
└── frontend/
    └── src/
        ├── App.jsx           # layout: map + side panel (simulate button, tickets)
        ├── MapView.jsx        # Leaflet map: colored pole dots, span/membership lines
        ├── FaultSimulatorDialog.jsx  # modal: search-select pole + scope + submit
        └── App.css
```

## Data model (SQLite tables in data.db)

**Static, generated once by generate_data.py — never written by the running app:**
- `substations(substation_id, lat, lon)`
- `feeders(feeder_id, substation_id, lat, lon)`
- `transformer_registry(dt_id, feeder_id, lat, lon, capacity_kva, households_served)`
- `pole_registry(pole_id, lat, lon, feeder_id, dt_id, seq_on_line, parent_pole_id, pole_type, ward, pincode, device_id)`
  — `seq_on_line`/`parent_pole_id` are NULL for ~60% of DTs (missing-topology problem, intentional)
- `ground_truth_topology(...)` — the real topology even where the above is NULL.
  Never read by the app; exists only for validating a future inference algorithm.

**Runtime, created and written by server.py as the system operates:**
- `telemetry(id, device_id, pole_id, event, energized, ts, seq, battery_mv, rssi, fw, received_at)`
  Raw append-only log. `UNIQUE(device_id, seq, event)` + `INSERT OR IGNORE` handles
  duplicate at-least-once deliveries.
- `device_last_seen(device_id, ts)` — last time ANY telemetry was heard from a
  device. Used for staleness detection. **Seeded to "now" for every device at
  server startup (see the fix below) — this is load-bearing.**
- `device_seq(device_id, seq)` — per-device monotonic sequence counter.
- `tickets(id, scope, target_id, affected_pole_ids, status, created_at)`

## Request/data flow

1. **`generate_data.py`** runs once, offline, produces `data.db`.
2. **`server.py` starts** → `ensure_runtime_tables()` creates the runtime
   tables if missing, then seeds `device_last_seen` with `ts = now()` for
   every device that has no row yet (`INSERT OR IGNORE`).
3. **Frontend polls `GET /api/network` every 5s.** This endpoint:
   - loads the graph (`graph_lib.build_graph`, cached in memory)
   - calls `detection.run_detection_pass(con, graph)`, which:
     a. computes every pole's status live from `telemetry` + `device_last_seen`
        (never from a stored status field — status is always derived)
     b. groups any `fault` poles by DT and opens one ticket per DT if none
        is already open for it
     c. auto-verifies any open ticket whose poles have all returned to `normal`
   - returns all nodes (poles/DTs/feeders/substations) + all edges (span/membership)
4. **User opens the fault simulator dialog**, searches for a pole
   (`GET /api/poles/search?q=...`), picks a scope, submits.
5. **`POST /api/simulate-fault {target_id, scope}`**:
   - finds every pole downstream of `target_id` in the graph (`nx.descendants`)
   - calls `telemetry_sim.generate_fault_events(...)`, which for each affected
     pole either produces a realistic `power_lost` message OR produces
     nothing (simulating a lost dying-message, a firmware-1.2 device that
     never sends one, or no device fitted at all) — matching the failure
     rates in the original spec
   - ingests those events through the exact same `ingest_batch()` function
     `/api/ingest` uses — the simulator is not a shortcut, it exercises the
     real pipeline
   - runs detection immediately so the response (and the next UI refresh)
     reflects it without waiting for the 5s poll
6. **Map re-renders** pole colors from `status` (green=normal, red=fault,
   grey=unknown/no device), and the ticket list updates.

## Status/color derivation rules (detection.py)

A pole is:
- **`fault`** if its most recent telemetry event is `power_lost` (nothing
  more recent supersedes it), **or** its device has sent nothing at all
  within `STALE_THRESHOLD_S` (currently 60s — shrunk from the real ~15min
  heartbeat + jitter + grace for interactive testing; see "Known limitations")
- **`unknown`** if it has no device fitted (~9% of poles by design) — no
  signal exists, so the system says so rather than guessing
- **`normal`** otherwise

## THE BUG THAT WAS FOUND (all poles showing red) — root cause + fix

**Symptom:** every pole shows `fault` even with no simulated fault.

**Root cause:** `device_last_seen` starts empty. `compute_pole_statuses`
treats "no row for this device" as `stale = True` (see the `seen_ts is None`
branch). With no baseline seeded, every device looks like it's been silent
forever, so every pole reads as stale → fault, from the very first request.

**The fix**, inside `ensure_runtime_tables()` in `server.py`:
```python
now = time.time()
device_ids = [r[0] for r in
              cur.execute("SELECT device_id FROM pole_registry WHERE device_id IS NOT NULL")]
cur.executemany("INSERT OR IGNORE INTO device_last_seen (device_id, ts) VALUES (?, ?)",
                 [(d, now) for d in device_ids])
```
Run once at startup, after the tables exist. `INSERT OR IGNORE` is important:
it only fills in devices that have never been mentioned — it must NOT
overwrite real staleness that has accumulated from actual simulated faults,
including across server restarts.

**If you still see all-red after this fix, check next, in order:**
1. Is `ensure_runtime_tables()` actually being called before the first
   request (not just defined)?
2. Is `data.db` on disk actually the same file the seeding step wrote to,
   and not a stale copy from before the fix?
3. Print `SELECT COUNT(*) FROM device_last_seen` right after startup — it
   should roughly match the number of poles with a `device_id` (~91% of
   total poles).
4. Confirm `STALE_THRESHOLD_S` in `telemetry_sim.py` matches what
   `detection.py` imports — they must be the same value/import, not two
   independent constants that drifted apart.

## Known v1 limitations (documented cuts, not accidents)

- **Ticket grouping is DT-level, not span-level.** All fault poles under one
  DT get one ticket, even if only one branch is actually affected. Natural
  next step: cluster fault poles by span-adjacency where topology is known
  (~40% of DTs), falling back to DT-level only where it's unknown.
- **No scheduled-outage integration yet.** The spec's load-shedding feed
  (section 4) isn't wired in, so a scheduled shutdown would currently be
  misread as a fault. Needs a check against the outage feed before opening
  a ticket.
- **No manual "crew marks resolved" step.** Tickets go straight from
  `detected` to auto-`verified` when telemetry recovers. The full
  `detected → acknowledged → crew_assigned → resolved → verified → closed`
  lifecycle needs a UI action for a human to claim/assign/mark-resolved
  before the telemetry-verification step makes sense.
- **`STALE_THRESHOLD_S = 60`** is a demo value for interactive testing. The
  spec's real numbers imply something like 30+ minutes (15min heartbeat
  interval + jitter + grace for one missed cycle) — needs to become a
  documented, deliberately-chosen production value, not left at 60.
- **Only poles *downstream* of the simulated target are affected**, and
  fault targeting assumes the graph correctly reflects topology (span vs
  membership) — a fault simulated on a `membership`-only DT still marks all
  its poles, since there is no finer-grained topology to target within it.
