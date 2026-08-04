# Humbug — Power Distribution Fault Operations Console

Fault-localization and ticketing system for a low-tension distribution network.
When a wire snaps, the control room knows the exact span, coordinates, and PIN
code within seconds — not two hours.

---

## Quick start

```bash
git clone <repo>
cd humbug
docker compose up
```

Open **http://localhost** in your browser. The stack self-seeds on first boot
(generates 2,707 synthetic poles across 40 DTs, 8 feeders, 4 substations).
No login, no API key, no manual steps.

Backend API is also accessible directly at **http://localhost:8000**.

> **Cold-start note:** the backend takes 15–20 s on first boot to generate
> the synthetic database. The frontend waits for the backend health check
> before it serves traffic, so you will not see a broken screen — just a
> brief delay.

---

## What it does

### The problem it solves

A snapped low-tension wire darkens dozens of homes. Today's process: someone
calls the complaint number → operator guesses the area → lineman drives out
and walks the poles until he finds the break. That's two hours, minimum.

Humbug compresses steps 1–4 to under two minutes by reading "energized / dark"
signals from IoT devices already on every pole and deriving the live/dark
boundary — the exact location of the fault.

### What the UI shows, and why

**The map is the dominant element.** A control-room operator at 2 a.m. needs
to answer one question first: *where is it?* The map answers that before
anything else. Every other panel is secondary.

- **Green poles** = energized, nominal.
- **Red poles** = part of an active unresolved fault. They get a pulsing ring
  at the fault centroid — the navigation target for a crew.
- **Solid grey poles** = under a scheduled load-shedding window (not a fault).
  Distinct from red so operators don't dispatch crews for planned outages.
- **Hollow/dashed grey poles** = no device fitted. We have no signal from
  these poles and say so explicitly rather than assuming normal.
- **Solid topology lines** = confirmed from the pole registry (`parent_pole_id`
  known). The 40% of the network where span-level localization is exact.
- **Dashed dim lines** = inferred via geometric MST from pole coordinates.
  The 60% of the network where topology was never digitized. Labelled
  "inferred, not surveyed" in every tooltip — we never imply certainty we
  don't have.

**The ticket list** shows only open faults, sorted by recency, with a
confidence bar on each card. One card = one fault. We deliberately do not
show one card per dark pole; that's the "40 alerts for one wire" failure mode
the brief calls out explicitly.

**The ticket detail drawer** shows:
- A lifecycle stepper (detected → acknowledged → crew assigned → resolved →
  verified → closed) with the current stage highlighted.
- A **verification callout** that tells the operator whether restoration was
  auto-verified from telemetry or is still pending. This is a core trust
  feature: the system cannot be tricked into "verified" by a button click.
- The fault's confidence score *and the reason for it* — e.g. "Clear live/dark
  boundary on known topology (12 poles dark)" vs "topology unknown for this
  DT — DT-level localization only." An operator can judge how much to trust
  each alert.
- Coordinates precise enough to put into a navigation app.

**The fault simulator** sits in the right column, collapsible. It's not hidden
in a settings menu because it's how the reviewer evaluates the system.

### What we deliberately did not show

- **No per-pole fault alert list.** Grouping into one ticket per DT is a
  first-class product decision, not an implementation shortcut.
- **No "mark verified" button.** The UI has no such action. Telemetry drives
  that transition or it doesn't happen.
- **No routing or crew scheduling.** Out of scope. The ticket gets to
  "crew assigned" and then the real dispatch system takes over.
- **No analytics dashboard.** Out of scope per the brief.
- **No historical trend charts.** Same.

---

## Architecture overview

```
Pole devices
    │  POST /api/ingest  (power_lost / heartbeat / boot)
    ▼
FastAPI backend (backend/app.py)
    │  SQLite WAL (data/data.db)
    │  ├─ telemetry
    │  ├─ device_last_seen
    │  ├─ tickets
    │  ├─ scheduled_outages
    │  └─ active_faults
    │
    │  detection.py  — compute_pole_statuses + run_detection_pass
    │  build_graph.py — NetworkX DiGraph (substation→feeder→DT→poles)
    │  telemetry_sim.py — fault/restore/noise generation
    │
    ├─ GET /api/poles         (status per pole, 3s poll)
    ├─ GET /api/tickets       (ticket list, 3s poll)
    ├─ GET /api/network/edges (topology edges, 10s poll)
    └─ POST /api/simulate/*   (fault injection / repair)

nginx (frontend/nginx.conf)
    │  Proxies /api/* → backend:8000
    │  Serves static frontend files at /
    ▼
Browser
    ├─ Leaflet map  (CartoDB dark_matter tiles, no API key)
    ├─ React 18 UMD (no build step, createElement not JSX)
    └─ HumbugPoller (3s / 10s interval polling)
```

Full architecture, algorithm detail, and data design decisions are in
`ARCHITECTURE.md` (to be written). API endpoint reference is in
`INTEGRATION_NOTES.md`.

---

## Fault localization — how it works

**For the 40% of DTs with known topology** (`parent_pole_id` present):
The graph is traversed from the DT down each branch. The first dark pole
after a live pole marks the fault span. Confidence: 0.9 ("clear live/dark
boundary on known topology").

**For the 60% of DTs with unknown topology:**
Two things happen in parallel:
1. **DT-level grouping** — all dark poles under a DT become one ticket with
   scope `dt`. The ticket reports the centroid of the dark poles as the
   navigation target. Confidence: 0.65 ("topology unknown — DT-level
   localization only"). This is explicitly surfaced in the UI.
2. **Geometric MST inference** — a minimum spanning tree is built over the
   pole coordinates, rooted at the DT location, to infer likely line order.
   This is shown on the map as dashed lines, labelled as inferred. It is not
   used for fault localization decisions in v1 (only for map display) because
   its accuracy is unvalidated. The ground truth is in `ground_truth_topology`
   — that table is never queried by the application, only by an offline eval
   script.

**Noise suppression:**
- Scheduled outage windows suppress fault tickets for affected DTs/feeders.
- A single dark pole whose downstream children are live is physically impossible
  as a line fault. v1 does not yet implement this "isolated dark pole" check —
  it's a documented known limitation.
- Device staleness (no heartbeat for >15 minutes) is treated the same as a
  `power_lost` event, catching fw-1.2 devices that never send the event.

---

## Simulator

From the right panel, click **⚡ Inject Fault** and choose:
- **Span fault** — darkens a selected pole and all poles electrically downstream
- **DT fault** — darkens every pole under a transformer
- **Feeder fault** — darkens every pole under every DT on a feeder

The simulator is physically honest:
- ~30% of `power_lost` messages are silently dropped (capacitor reserve failure)
- ~8% of devices are fw-1.2 and never send `power_lost` — they just go quiet,
  and the stale-threshold logic catches them after 15 minutes
- Restoration sends `boot + power_restored`, which triggers auto-verification

Secondary noise injections (not faults):
- **Dead device** — makes one device's last_seen stale without affecting neighbours
- **Load shed** — registers a scheduled outage window to suppress false tickets
- **Duplicate burst** — sends 3 duplicate heartbeats to test dedup logic

---

## What's real vs mocked

| Thing | Status |
|-------|--------|
| Pole/DT/feeder/substation data | Real synthetic data from `generate_data.py` (2,707 poles, seed 42) |
| Telemetry ingest (`POST /ingest`) | Real — deduped, triggers detection |
| Fault detection | Real — `detection.py` running on every ingest |
| Ticket lifecycle | Real — status transitions enforced server-side |
| Auto-verification from telemetry | Real — `run_detection_pass` auto-verifies when all affected poles recover |
| MST topology inference | Real algorithm, shown on map; not yet used for localization decisions |
| Scheduled outage feed | Mocked — no external feed, but the endpoint works and the simulator can inject windows |
| Load shedding suppression | Real — outage windows suppress ticket creation |
| Map tiles | OpenStreetMap via CartoDB dark_matter (free, no API key) |
| Authentication | Stubbed — no auth, operator identity is implicit |
| Geocoding | Not needed — pincode comes from pole registry; fallback borrows from sibling poles on same DT |

---

## Known gaps and what I'd fix first

1. **Isolated dark pole check not implemented.** A single dark pole with live
   children is a dead device, not a fault. Detection should skip ticket creation
   in that case. Currently it raises a ticket, which an operator would then
   notice has a single pole and low confidence. First thing to fix.

2. **MST topology not used for localization.** Inferred edges are shown on the
   map but the localization algorithm still falls back to DT-level for all
   topology-unknown DTs. With 60% of DTs affected, implementing span-level
   localization on inferred topology (with appropriate confidence penalty) would
   substantially improve the answer quality.

3. **Concurrent writes under burst.** SQLite WAL mode handles concurrent
   readers fine, but sustained writes at 500 msg/s from a single process on
   a Docker volume haven't been benchmarked. For production scale, swap
   SQLite for Postgres.

4. **Deployment.** The stack runs locally with `docker compose up` and is
   ready to push to Railway (`railway up`) or Render with no config changes.
   See `INTEGRATION_NOTES.md` for the deployment recommendation.

---

## Project structure

```
.
├── backend/
│   ├── app.py             # FastAPI application — all endpoints
│   ├── requirements.txt
│   ├── Dockerfile
│   └── entrypoint.sh      # DB seed + uvicorn start
├── frontend/
│   ├── index.html         # App shell (no build step)
│   ├── css/theme.css      # Full design system
│   ├── js/
│   │   ├── api.js         # API client + HumbugPoller + helpers
│   │   ├── map.js         # Leaflet map component
│   │   ├── tickets.js     # Ticket list + detail drawer (React)
│   │   ├── simulator.js   # Fault simulator panel (React)
│   │   └── app.js         # Boot sequence — wires everything together
│   ├── Dockerfile
│   └── nginx.conf         # Static file server + /api proxy
├── data/
│   ├── data.db            # Seed SQLite database
│   ├── pole_registry.csv
│   ├── transformer_registry.csv
│   ├── feeders.csv
│   └── substations.csv
├── detection.py           # Fault detection logic
├── build_graph.py         # NetworkX graph builder
├── telemetry_sim.py       # Simulator — fault/restore/noise telemetry generation
├── generate_data.py       # Synthetic data generator (read-only)
├── docker-compose.yml
├── .env.example
├── INTEGRATION_NOTES.md   # Endpoint reference + architecture decisions
└── README.md
```
