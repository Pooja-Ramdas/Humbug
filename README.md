# Humbug — Power Distribution Fault Operations Console

Humbug is an end-to-end fault-localization and operational control system designed for low-tension (LT) electrical distribution networks. By ingesting IoT sensor signals ("power_lost", "power_restored", heartbeats) from pole-top monitors and applying graph-based downstream fault propagation on a radial network tree, Humbug pinpoint-localizes physical wire snaps and outages down to the exact pole span and GPS coordinates within seconds.

---

## Documentation Index

- [ARCHITECTURE.md](ARCHITECTURE.md) — Technical design, NetworkX graph representation, fault detection algorithm, database schema, and mathematical complexity.
- [DEPLOYMENT.md](DEPLOYMENT.md) — Prerequisites, Docker deployment, local setup, configuration options, and troubleshooting guide.
- [DECISIONS.md](DECISIONS.md) — Engineering decision log, trade-offs, architecture decisions, technical debt, and 2-week roadmap.
- [AI-WORKFLOW.md](AI-WORKFLOW.md) — AI delegation strategy, manual bug corrections, tools used, and estimated code attribution.

---

## Key Features

- **Radial Downstream Fault Propagation**: When an electrical wire snaps, the initiating faulty pole and **100% of downstream poles** on the line automatically turn RED (`fault`). No green poles can exist inside an active fault span.
- **Complete Span Recovery Propagation**: When a fault is repaired, 100% of affected downstream poles immediately return to GREEN (`normal`). Zero residual red poles remain.
- **Corroborated Staleness & Noise Suppression**: Isolated single dead sensors are marked GREY (`device_fault`) to prevent false-positive ticket dispatch. At least 2 corroborating stale/fault poles under the same transformer are required to declare a line fault.
- **Single Tight Fault Indicator Circle**: The map displays **exactly ONE tight 12m dotted circle** centered strictly on the initiating (first) faulty pole of each active fault span.
- **Background Heartbeat Refresh**: Continuous background thread refreshes healthy IoT device heartbeats, eliminating wall-clock time drift and preventing false fleet-wide staleness.
- **Interactive Control Room Console**: Dark-themed map view (Leaflet.js Canvas), real-time ticket lifecycle drawer (Detected → Acknowledged → Assigned → Resolved → Verified → Closed), and an embedded fault simulator panel.
- **Topology Inference Engine**: For 60% of transformers lacking surveyed line ordering, geometric Prim's Minimum Spanning Tree (MST) infers likely line connectivity, visually rendered as dashed lines.

---

## Technologies Used

- **Backend**: Python 3.11, FastAPI / Flask, NetworkX (Graph Algorithms), SQLite (WAL mode).
- **Frontend**: Vanilla JavaScript (ES6+), Leaflet.js (Canvas CircleMarker rendering), React 18 (UMD, no build step), Three.js (3D Logo animation), Vanilla CSS (Custom tokens).
- **Infrastructure**: Docker, Nginx, Docker Compose.

---

## One-Command Startup (Docker)

To run the complete stack (Backend API + Nginx Static Frontend) with zero manual configuration:

```bash
git clone https://github.com/Pooja-Ramdas/Humbug.git
cd Humbug
docker compose up --build
```

Access points:
- **Control Room Console**: [http://localhost](http://localhost)
- **Backend API Direct**: [http://localhost:8000](http://localhost:8000)

*Note: On first startup, the database self-seeds with 2,750 synthetic poles across 250 transformers, 25 feeders, and 5 substations.*

---

## Local Development Instructions (Without Docker)

### 1. Prerequisites
- Python 3.11+
- Git

### 2. Backend Setup
```bash
# Clone repository
git clone https://github.com/Pooja-Ramdas/Humbug.git
cd Humbug

# Create and activate virtual environment
python -m venv venv
# On Windows:
venv\Scripts\activate
# On Linux/macOS:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run backend server
python server.py
```
The backend server runs on `http://localhost:8000`.

### 3. Frontend Setup
In a separate terminal, serve the `frontend/` directory using any HTTP server:

```bash
cd frontend
python -m http.server 8080
```
Open **[http://localhost:8080](http://localhost:8080)** in your browser.

---

## Public Deployment & Demo

- **Public Deployment URL**: `http://localhost` (Configurable via Docker or cloud hosting like Render/Railway/Fly.io).
- **Demo Video**: *[Placeholder: Demo Video Link]*

---

## Repository Structure

```
.
├── backend/
│   ├── app.py             # FastAPI application and lifespan setup
│   ├── requirements.txt   # Python dependencies
│   ├── Dockerfile         # Backend container definition
│   └── entrypoint.sh      # Container boot script
├── frontend/
│   ├── index.html         # Main console shell
│   ├── css/
│   │   └── theme.css      # Design system & dark theme rules
│   ├── js/
│   │   ├── api.js         # API client & poller
│   │   ├── map.js         # Leaflet map renderer
│   │   ├── tickets.js     # Ticket drawer & lifecycle component
│   │   ├── simulator.js   # Fault simulation panel
│   │   └── app.js         # Main application controller
│   ├── Dockerfile         # Nginx frontend container
│   └── nginx.conf         # Nginx static server & API proxy
├── data/
│   ├── data.db            # SQLite database (WAL mode)
│   ├── pole_registry.csv  # Pole location & device mapping
│   ├── transformer_registry.csv # Transformer metadata
│   ├── feeders.csv        # Feeder definitions
│   └── substations.csv    # Substation definitions
├── detection.py           # Fault detection & downstream propagation algorithm
├── build_graph.py         # NetworkX topology graph builder
├── telemetry_sim.py       # Fault, recovery, and telemetry simulator
├── generate_data.py       # Synthetic topology generator script
├── server.py              # Standalone Flask server
├── docker-compose.yml     # Multi-container orchestrator
├── .env.example           # Environment variables reference
├── README.md              # Project overview
├── ARCHITECTURE.md        # Technical architecture document
├── DEPLOYMENT.md          # Deployment & troubleshooting guide
├── DECISIONS.md           # Engineering decision log
└── AI-WORKFLOW.md         # AI tool utilization & breakdown
```
