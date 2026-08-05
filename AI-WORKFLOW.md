# Humbug — AI Collaboration & Engineering Workflow

This document details the AI-assisted engineering workflow, delegation strategy, manual corrections, AI hallucinations, and attribution breakdown for the Humbug codebase.

---

## 1. AI Tools Utilized

- **Gemini Flash**: Used for rapid boilerplate code generation, initial schema design, API endpoint templates, and script creation (`generate_data.py`).
- **Claude Opus**: Used for complex architectural reasoning, NetworkX graph algorithms, state machine lifecycle planning, and debugging multi-step logic.

---

## 2. Delegation Strategy

- **AI Delegated Tasks**:
  - Synthetic network topology generation (`generate_data.py`).
  - Standard database table schema definitions (`data.db` tables).
  - Initial FastAPI and Flask API endpoint scaffolding (`backend/app.py`, `server.py`).
  - Standard CRUD queries and helper functions.

- **Manual Engineering & Human Ownership**:
  - **User Interface & Experience**: Designed and implemented the operator control room console (`frontend/css/theme.css`, `frontend/js/app.js`, `frontend/js/map.js`, `frontend/js/tickets.js`) manually to achieve precise layout control, dark-mode ergonomics, and usability.
  - **Core Fault Localization Logic**: Designed the 3-pass detection engine in `detection.py`, including the 2+ peer corroboration rule for suppressing dead IoT modem noise.
  - **Graph Propagation Integrity**: Implemented NetworkX directed graph traversals and line sequence order enforcement (`seq_on_line`).
  - **System Verification & Testing**: Created custom Python verification scripts (`scratch/test_*.py`) to systematically test edge cases and eliminate AI hallucinations.

---

## 3. Examples Where AI Was Incorrect

During development, relying solely on AI outputs resulted in critical logical errors that required manual debugging and structural correction:

1. **Incorrect Downstream Fault Propagation**:
   - *AI Mistake*: AI initially generated downstream checks that evaluated poles independently or modified status based only on immediate neighbor nodes. This produced physically impossible states like `G - G - R - G - G` (a green energized pole downstream of a snapped wire).
   - *Manual Fix*: Rewrote Pass 3 of `compute_pole_statuses` in `detection.py` to use `nx.descendants(graph, fp)` combined with explicit `seq_on_line >= fp.seq_on_line` line sequence filtering, guaranteeing that 100% of downstream poles turn red on a fault.

2. **Fault Recovery Propagation & Residual Red Poles**:
   - *AI Mistake*: The AI introduced a random 15% drop rate during restoration telemetry injection, leaving residual red poles on a repaired line (`G - G - R - G - R`).
   - *Manual Fix*: Corrected `inject_restore_telemetry` in `telemetry_sim.py` to target 100% of affected downstream poles, delete prior `power_lost` events, and refresh `device_last_seen` timestamps so that repair resolves 100% of poles back to GREEN (`G - G - G - G - G - G - G`).

3. **Fleet-Wide Staleness Drift**:
   - *AI Mistake*: AI seeded `device_last_seen` once at startup without a continuous heartbeat mechanism. After 60 seconds of idle wall-clock time, every untouched device became stale simultaneously, causing the entire network to drift RED.
   - *Manual Fix*: Designed and implemented `refresh_healthy_heartbeats` background thread in `detection.py` and `backend/app.py` to continuously refresh healthy device timestamps while excluding active fault devices.

---

## 4. Best AI-Assisted Work

The most effective AI collaboration occurred during **iterative interface refinement and CSS styling**:
- AI excel at generating clean CSS design tokens, modern typography rules, and responsive flexbox layouts.
- Rapid prototyping of Three.js canvas animations and Leaflet tooltip formatting benefited significantly from AI suggestion cycles.

---

## 5. Contribution & Code Attribution Breakdown

| Engineering Area | AI Contribution | Manual Contribution | Primary Responsibility |
| :--- | :---: | :---: | :--- |
| **Synthetic Topology Data** | 85% | 15% | AI generated generator script; manual parameters. |
| **Database Schemas & Boilerplate** | 75% | 25% | AI drafted SQL tables; manual WAL tuning. |
| **Fault Localization & Graph Logic** | 35% | 65% | AI provided initial draft; manual rewrite of 3-pass logic. |
| **Simulation & Recovery Engine** | 40% | 60% | AI generated event templates; manual fix for 100% recovery. |
| **Frontend UI, CSS & UX Design** | 30% | 70% | Manual design system, layout, and Leaflet integration. |
| **Overall Project Codebase** | **~60% AI** | **~40% Manual** | **Reflective Human-in-the-Loop Engineering** |
