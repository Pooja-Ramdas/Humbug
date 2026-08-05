# Humbug — Technical Architecture & Data Design

This document details the system architecture, mathematical models, graph representations, fault localization algorithms, and database design of the Humbug Power Distribution Fault Operations Console.

---

## 1. System Architecture Diagram

                                   KSPDB FAULT LOCALIZATION SYSTEM

──────────────────────────────────────────────────────────────────────────────────────────────────────

                  OFFLINE DATA GENERATION
         (Executed once before deployment)

        ┌─────────────────────────────┐
        │      generate_data.py       │
        │                             │
        │ • Synthetic Network         │
        │ • Poles                     │
        │ • Feeders                   │
        │ • Distribution Transformers │
        │ • IoT Devices               │
        └──────────────┬──────────────┘
                       │
                       ▼
              ┌──────────────────┐
              │    SQLite DB      │
              │     data.db       │
              │                  │
              │ Static Tables    │
              │ Runtime Tables   │
              └─────────┬────────┘


═══════════════════════════════════════════════════════════════════════════════════════════════════════

                         BACKEND (Flask)

        ┌──────────────────────────────────────────────┐
        │              server.py                       │
        │                                              │
        │ REST API                                     │
        │ /api/network                                │
        │ /api/ingest                                 │
        │ /api/simulate-fault                         │
        │ /api/poles/search                           │
        └──────┬──────────────────────────────────────┘
               │
               │
               ▼

      ┌───────────────────────────────┐
      │        graph_lib.py           │
      │                               │
      │ Builds NetworkX Radial Graph  │
      │ Cached in Memory              │
      └──────────────┬────────────────┘
                     │
                     ▼

      ┌───────────────────────────────┐
      │      telemetry_sim.py         │
      │                               │
      │ Simulates                     │
      │ • Power Lost                  │
      │ • Missing Packets             │
      │ • Dead Devices                │
      │ • Firmware Variations         │
      └──────────────┬────────────────┘
                     │
                     ▼

      ┌───────────────────────────────┐
      │       detection.py            │
      │                               │
      │ • Compute Pole Status         │
      │ • Fault Localization          │
      │ • Ticket Generation           │
      │ • Auto Verification           │
      └──────────────┬────────────────┘
                     │
                     ▼

              SQLite Runtime Tables
         telemetry
         device_last_seen
         device_seq
         tickets


═══════════════════════════════════════════════════════════════════════════════════════════════════════

                      FRONTEND (React)

          ┌─────────────────────────────┐
          │         App.jsx             │
          │                             │
          │ Dashboard Layout            │
          └─────────────┬───────────────┘
                        │
         ┌──────────────┼────────────────┐
         │              │                │
         ▼              ▼                ▼

 ┌────────────────┐ ┌────────────────┐ ┌─────────────────────┐
 │  MapView.jsx   │ │ Fault Simulator│ │ Ticket Side Panel   │
 │                │ │                │ │                     │
 │ Leaflet Map    │ │ Inject Fault   │ │ Active Incidents    │
 │ Pole Status    │ │ Search Pole    │ │ Telemetry           │
 │ Span Lines     │ │                │ │ Load Shedding       │
 └────────────────┘ └────────────────┘ └─────────────────────┘


═══════════════════════════════════════════════════════════════════════════════════════════════════════

                         USER WORKFLOW

Operator
     │
     ▼
Inject Fault
     │
     ▼
POST /api/simulate-fault
     │
     ▼
Telemetry Simulation
     │
     ▼
Telemetry Stored
     │
     ▼
Detection Algorithm
     │
     ▼
Pole Status Computed
     │
     ▼
Fault Tickets Created
     │
     ▼
GET /api/network
     │
     ▼
React Refresh
     │
     ▼
Leaflet Map Updated
     │
     ▼
Operator Sees
• Red Faulted Spans
• Green Healthy Poles
• Grey Unknown Devices
• Active Fault Count
• Ticket List


## 2. End-to-End Data Flow

1. **Telemetry Event Ingestion**: Pole devices or the simulator emit JSON payloads containing `device_id`, `pole_id`, `event` (`power_lost`, `power_restored`, `boot`, `heartbeat`), `seq`, and `ts`.
2. **Deduplication & Storage**: The backend executes a deduplication query on `(pole_id, seq)`. Unique events are appended to `telemetry` and `device_last_seen` is updated.
3. **Graph-Based Detection Pass**:
   - `compute_pole_statuses` runs a 3-pass analysis over the NetworkX topology graph and telemetry records.
   - Evaluates active load shed windows, missing sensors, dying gasps, and device staleness.
   - Applies 2-peer corroboration to suppress single dead modem noise.
   - Propagates fault status (`"fault"`, red) downstream along the radial tree graph.
4. **Ticket Generation & Enrichment**:
   - New fault locations trigger ticket creation in `tickets`.
   - `compute_ticket_metadata` calculates the centroid GPS coordinates, determines confidence score (`0.40` to `0.90`), and generates a human-readable explanation.
5. **Frontend Polling & Rendering**:
   - `HumbugPoller` polls `/api/network/status` and `/api/tickets` every 3 seconds.
   - `map.js` updates marker styles on Leaflet canvas without DOM node destruction.
   - `tickets.js` updates active ticket cards and the lifecycle drawer.

---

## 3. Telemetry Ingestion Pipeline & Deduplication

### Ingestion Endpoints
- `POST /api/ingest`: Accepts a single telemetry payload.
- `POST /api/ingest/batch`: Accepts an array of telemetry payloads for burst scenarios.

### Deduplication & Out-of-Order Handling
The ingestion pipeline enforces strict idempotency using a compound unique index on `(pole_id, seq)`:

```sql
SELECT id FROM telemetry WHERE pole_id = ? AND seq = ?
```

- If `(pole_id, seq)` already exists in `telemetry`, the duplicate event is silently acknowledged (`200 OK` with `"duplicate": true`) and ignored.
- Timestamp ordering (`ts`) ensures that out-of-order messages do not overwrite newer status updates.

---

## 4. Storage Model & SQLite Schema Overview

Humbug uses SQLite in **Write-Ahead Logging (WAL)** mode (`PRAGMA journal_mode=WAL`) to allow concurrent non-blocking reads while writes occur.

```sql
-- Core Topology Registries (Populated from CSV seed)
CREATE TABLE pole_registry (
    pole_id TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    dt_id TEXT NOT NULL,
    feeder_id TEXT NOT NULL,
    ward TEXT,
    pincode TEXT,
    device_id TEXT,
    seq_on_line INTEGER,
    parent_pole_id TEXT
);

CREATE TABLE transformer_registry (
    dt_id TEXT PRIMARY KEY,
    feeder_id TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    capacity_kva INTEGER,
    households_served INTEGER
);

CREATE TABLE feeders (
    feeder_id TEXT PRIMARY KEY,
    substation_id TEXT NOT NULL,
    voltage_kv REAL,
    name TEXT
);

CREATE TABLE substations (
    substation_id TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    name TEXT,
    capacity_mva REAL
);

-- Runtime Tables
CREATE TABLE telemetry (
    id TEXT PRIMARY KEY,
    pole_id TEXT NOT NULL,
    device_id TEXT,
    event TEXT NOT NULL,
    energized INTEGER,
    ts REAL,
    seq INTEGER,
    battery_mv INTEGER,
    rssi INTEGER,
    fw TEXT,
    received_at REAL
);

CREATE TABLE device_last_seen (
    device_id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    pole_id TEXT
);

CREATE TABLE tickets (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,         -- 'pole' | 'dt' | 'feeder'
    target_id TEXT NOT NULL,
    affected_pole_ids TEXT,     -- CSV list of pole IDs
    status TEXT NOT NULL,        -- 'detected'|'acknowledged'|'assigned'|'resolved'|'verified'|'closed'
    confidence REAL,             -- 0.0 to 1.0
    confidence_reason TEXT,
    lat REAL,
    lon REAL,
    pincode TEXT,
    feeder_id TEXT,
    created_at REAL,
    updated_at REAL,
    acknowledged_at REAL,
    crew_assigned_at REAL,
    resolved_at REAL,
    verified_at REAL,
    closed_at REAL
);

CREATE TABLE scheduled_outages (
    id TEXT PRIMARY KEY,
    scope TEXT NOT NULL,
    target_id TEXT NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    status TEXT DEFAULT 'active',
    reason TEXT
);

CREATE TABLE active_faults (
    id TEXT PRIMARY KEY,
    fault_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    injected_at REAL NOT NULL,
    restored INTEGER DEFAULT 0
);
```

---

## 5. Network Topology Representation

Network topology is built into a NetworkX directed graph (`nx.DiGraph`) via `build_graph.py`:

```text
Substation (Root)
  └── Feeder
       └── Distribution Transformer (DT)
            ├── Pole 1 (parent_pole_id = NULL, seq_on_line = 1)
            │    └── Pole 2 (parent_pole_id = Pole 1, seq_on_line = 2)
            │         └── Pole 3 (parent_pole_id = Pole 2, seq_on_line = 3)
```

- **Surveyed Topology (40% of DTs)**: Connected via explicit directed edges (`parent_pole_id` -> `pole_id`).
- **Unsurveyed Topology (60% of DTs)**: Connected via star membership edges (`dt_id` -> `pole_id`). Inferred line ordering is calculated geometrically via Prim's Minimum Spanning Tree (MST) for UI rendering.

---

## 6. Fault Localization Algorithm (`detection.py`)

The status calculation engine executes a 3-pass evaluation:

### Pass 1: Raw Device Assessment
- **Load Shedding**: Checks `scheduled_outages`. If an active load-shedding window covers the DT/feeder, poles are marked `"load_shedding"` (yellow).
- **Missing Sensor**: If `device_id` is NULL, pole status is `"unknown"` (grey).
- **Dying Gasp**: If latest telemetry event is `'power_lost'`, pole status is `"fault"` (red candidate).
- **Staleness Check**: If `now - device_last_seen.ts > STALE_THRESHOLD_S` (60s), pole status is `"stale_pending"`.

### Pass 2: Corroboration & Single Dead Sensor Suppression
- For poles in `"stale_pending"`, count how many peer poles on the same DT are also in `"fault"` or `"stale_pending"`.
- If `corroboration_count >= 2`: Converted to `"fault"` (line fault).
- If `corroboration_count < 2`: Marked as `"device_fault"` (grey, single dead IoT modem). No ticket generated.

### Pass 3: Radial Downstream Fault Propagation
- For every pole `fp` in `"fault"` status:
  1. All NetworkX graph descendants `nx.descendants(graph, fp)` are set to `"fault"`.
  2. All poles under the same `dt_id` with `seq_on_line >= fp.seq_on_line` are set to `"fault"`.
- **Result**: Guarantees zero green poles downstream of a fault root (`G - G - R - R - R - R`).

---

## 7. Active Fault Location Counting & Ticket Grouping

The active fault counter (`count_active_faults`) determines root cause locations:
- Filters all poles currently in `"fault"` status.
- Identifies root fault poles that have **no predecessor pole** also in `"fault"` status.
- A continuous downstream outage across 1, 9, or 50 poles maps to **exactly 1 active fault location**.
- Ticket grouping aggregates all dark poles under that root cause into a single ticket, avoiding alert fatigue.

---

## 8. Simultaneous Fault Handling

Multiple independent wire breaks across different feeders or transformers are identified as distinct fault roots by `count_active_faults`. Each distinct fault root generates its own ticket with a unique ID, independent centroid coordinates, and separate lifecycle tracking.

Fault Detection Pipeline

Telemetry
      │
      ▼
Device Last Seen Check
      │
      ▼
Status Derivation
      │
      ├── Normal
      ├── Fault
      └── Unknown
      │
      ▼
Graph Traversal
      │
      ▼
Downstream Fault Localization
      │
      ▼
Incident Grouping (DT)
      │
      ▼
Ticket Generation

---

## 9. Confidence Calculation Heuristic

Every ticket is assigned a confidence score ($0.0$ to $1.0$) based on network topology certainty:

| Condition | Confidence | Reason Explanation |
| :--- | :---: | :--- |
| Known topology, $\ge 2$ dark poles | `0.90` | *"Clear live/dark boundary on known topology (N poles dark)"* |
| Unknown topology, $\ge 2$ dark poles | `0.65` | *"N poles dark, topology unknown for this DT — DT-level localization only"* |
| Entire DT dark ($\ge 95\%$ poles) | `0.55` | *"Entire DT dark (N/M poles) — likely DT-level fault or feeder issue"* |
| Single dark pole | `0.40` | *"Single dark pole — could be isolated device failure"* |

---

## 10. Handling Transformers Without Recorded Pole Ordering

For 60% of DTs where `seq_on_line` is NULL:
1. **Localization**: Grouped at the DT level (`scope="dt"`), with ticket coordinates set to the spatial centroid of dark poles.
2. **Visual MST Inference**: `infer_topology_mst` builds a Prim's Minimum Spanning Tree over pole GPS coordinates rooted at the DT. Inferred edges are sent to the Leaflet map as dashed dim lines (`edge_type="inferred"`).

---

## 11. Computational Complexity

- **Topology Graph Construction**: $O(V + E)$ built once at startup ($V = 2,750, E \approx 3,000$).
- **Detection Pass**: $O(V + E)$ network traversal (~5–15 ms execution time).
- **Map Update**: $O(V)$ canvas style updates with zero DOM element creation.

---

## 12. Known Limitations & Edge Cases

1. **High-Frequency Writes**: SQLite WAL handles concurrent reads well, but sustained writes $>500$ msg/s should be migrated to PostgreSQL in production.
2. **Inferred MST for Decision Making**: Inferred MST edges are currently used for visual UI rendering only, not for algorithmic fault boundary decisions.

---

## 13. API Endpoint Reference

| Method | Endpoint | Description |
| :--- | :--- | :--- |
| `GET` | `/health` | Server health check and quick stats |
| `GET` | `/stats` | System statistics (pole counts, fault counts) |
| `POST` | `/ingest` | Ingest single pole telemetry payload |
| `POST` | `/ingest/batch` | Batch ingest telemetry payloads |
| `GET` | `/poles` | Fetch all poles with fresh status |
| `GET` | `/network/topology` | Fetch static network topology (lat, lon, dt_id) |
| `GET` | `/network/edges` | Fetch topology edges (surveyed and inferred) |
| `GET` | `/tickets` | Fetch open fault tickets |
| `GET` | `/tickets/<id>` | Fetch specific ticket details |
| `POST` | `/tickets/<id>/acknowledge` | Transition ticket to `acknowledged` |
| `POST` | `/tickets/<id>/assign-crew` | Transition ticket to `assigned` |
| `POST` | `/tickets/<id>/resolve` | Transition ticket to `resolved` |
| `POST` | `/api/simulate-fault` | Inject fault (span, dt, feeder) |
| `POST` | `/api/simulate-restore` | Restore fault (clear telemetry, close ticket) |
| `POST` | `/api/simulate-load-shed` | Schedule load shedding outage window |

---

## 14. UI Design Rationale

- **Map-Centric Layout**: The Leaflet map occupies the primary screen real estate. Control room operators require immediate visual spatial context.
- **Color Token System**:
  - Green (`#39ff14`): Energized / Nominal.
  - Red (`#ff0055`): Active Line Fault.
  - Yellow (`#ffe600`): Planned Load Shedding.
  - Grey (`#8a8a8a`): IoT Device Fault or No Device Fitted.
- **Single Fault Circle**: Exactly one tight 12m dotted circle encircles the initiating pole of each fault span.

---

## 15. AI Feature Description

- **Automated Verification**: Telemetry-driven auto-verification prevents manual operator override until physical power restoration is confirmed by `boot` and `power_restored` signals.
- **Confidence Reasoning**: Human-readable confidence explanations generated dynamically per ticket.

