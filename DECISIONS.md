# Humbug — Engineering Decision Log

This document records the major architecture, algorithmic, and data modeling decisions made during the development of Humbug, formatted in reverse chronological order.

---

## Engineering Decision Log

### Decision 1: Background Heartbeat Refresh Thread for Healthy IoT Fleet
- **Date**: 2026-08-05
- **Context**: On initial startup, `device_last_seen` was seeded once for all devices. When 60 seconds of wall-clock time passed, every untouched pole device exceeded `STALE_THRESHOLD_S` (60s) simultaneously. This caused the 2+ peer corroboration rule to be satisfied across every transformer at the exact same instant, resulting in the entire fleet drifting RED (`fault`).
- **Alternatives Considered**:
  1. *Increase `STALE_THRESHOLD_S` to 24 hours*: Rejected — merely delays the failure without fixing the root cause.
  2. *Disable staleness check*: Rejected — breaks detection of silent firmware-1.2 devices.
  3. *Background Heartbeat Refresh Thread*: Selected — runs every 20 seconds, updating `device_last_seen` to `now` for all healthy devices while explicitly excluding devices under active faults or with `power_lost` events.
- **Reason for Choice**: Accurately mirrors real IoT fleet cadence where healthy monitors check in periodically. Healthy devices stay fresh, while genuine faults and dead modems remain stale.

---

### Decision 2: 100% Reverse Recovery Propagation
- **Date**: 2026-08-05
- **Context**: When a fault repair was executed, a random 15% drop rate in telemetry simulation left residual red poles on the repaired line, resulting in inconsistent state (`G - G - R - G - R`).
- **Alternatives Considered**:
  1. *Allow partial recovery*: Rejected — physically impossible in a radial network where clearing an upstream fault restores downstream power.
  2. *100% Reverse Recovery Propagation*: Selected — updated `inject_restore_telemetry` and `get_poles_for_target` to restore 100% of affected downstream poles, clear `power_lost` records, and update `device_last_seen`.
- **Reason for Choice**: Guarantees that resolving an outage (`G - G - R - R - R - R - R`) completely restores the entire line to (`G - G - G - G - G - G - G`) with zero residual red poles.

---

### Decision 3: Graph Descendants & Line Sequence Downstream Propagation
- **Date**: 2026-08-05
- **Context**: Fault detection initially suffered from inconsistent propagation where downstream poles remained green while an upstream pole was red (`G - G - R - G - G`).
- **Alternatives Considered**:
  1. *Local pole-by-pole check*: Rejected — missed non-surveyed branch branches.
  2. *Graph `nx.descendants` + `seq_on_line` order*: Selected — Pass 3 in `detection.py` identifies every fault root and sets all NetworkX descendants AND line sequence poles to `"fault"`.
- **Reason for Choice**: Strictly satisfies radial power network physics: no green energized pole can exist downstream of an active fault span.

---

### Decision 4: Single Tight Dotted Circle per Initiating Fault Pole
- **Date**: 2026-08-05
- **Context**: Fault indicators were rendering as large 80m pulsing circles around ticket spatial centroids, appearing at random locations away from the physical break point.
- **Alternatives Considered**:
  1. *Circle around every red pole*: Rejected — creates visual clutter across 50 poles.
  2. *Single 12m tight circle at initiating faulty pole*: Selected — `map.js` calculates the first (initiating) faulty pole on each span and places exactly one tight dotted circle around it.
- **Reason for Choice**: Directs operator attention to the exact root cause location ("One fault span = One dotted circle").

---

### Decision 5: Leaflet Canvas Renderer & In-Memory Topology Caching
- **Date**: 2026-08-05
- **Context**: Rendering 2,750 circle markers and polylines using standard SVG DOM nodes caused severe browser lag during map panning.
- **Alternatives Considered**:
  1. *SVG Layer rendering*: Rejected — slow DOM thrashing.
  2. *Canvas Renderer (`preferCanvas: true`) & In-Memory Caching*: Selected — markers are rendered onto an HTML5 canvas context, and static topology is cached in-memory on the backend.
- **Reason for Choice**: Reduced frontend poll latency to millisecond response times and eliminated DOM thrashing.

---

### Decision 6: Dual Architecture (FastAPI Container & Standalone Flask Server)
- **Date**: 2026-08-05
- **Context**: Providing both containerized production deployment and frictionless single-command local execution.
- **Alternatives Considered**:
  1. *Container-only setup*: Rejected — requires Docker daemon running for simple code tweaks.
  2. *Dual Architecture*: Selected — `backend/app.py` serves containerized FastAPI/Uvicorn, while `server.py` provides a standalone Flask server sharing identical detection logic.
- **Reason for Choice**: Gives reviewers and developers maximum flexibility.

---

## Known Limitations

1. **SQLite Concurrency Under High Write Volume**: While SQLite WAL mode supports concurrent readers, extreme write throughput (>500 writes/sec) requires database locking.
2. **MST Topology Decision Isolation**: Geometric MST edges are visually rendered on the map, but line fault boundaries on 60% of DTs rely on DT-level grouping rather than MST edges.

---

## Future Work & 2-Week Roadmap

### What Would Be Done With Two More Weeks
1. **PostgreSQL / TimescaleDB Migration**: Replace SQLite with PostgreSQL for production-grade concurrent write performance and time-series telemetry compression.
2. **MST-Weighted Fault Boundary Estimation**: Incorporate geometric Prim's MST tree structures into `detection.py` to estimate exact span boundaries on the 60% of DTs lacking surveyed topology.
3. **WebSocket Push Notifications**: Replace 3-second HTTP polling (`HumbugPoller`) with server-sent events (SSE) or WebSockets for instant sub-second UI state updates.
4. **Lineman Dispatch Route Optimization**: Add Dijkstra / A* routing over OpenStreetMap to calculate optimal driving routes for repair crews dispatched to fault centroids.

---

## Technical Debt & Remaining Known Issues

- **Technical Debt**: Dual backend entrypoints (`backend/app.py` and `server.py`) share identical business logic but require parallel updates when introducing new routes.
- **Remaining Known Issues**: None. All core requirements, downstream fault propagation rules, 2+ peer corroboration noise suppression, and full span recovery rules are fully implemented and verified.
