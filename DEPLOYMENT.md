# Humbug — Deployment & Operating Guide

This document provides step-by-step instructions for deploying, running, configuring, and troubleshooting the Humbug Power Distribution Fault Operations Console in both containerized and standalone environments.

---

## 1. System Requirements & Prerequisites

### Minimum Hardware
- **CPU**: 2 cores
- **RAM**: 4 GB
- **Disk Space**: 2 GB free

### Software Requirements
- **Docker**: Version 20.10+
- **Docker Compose**: Version 2.0+
- **Python** (for local non-Docker development): Python 3.11+
- **Browser**: Chrome, Firefox, Edge, or Safari with WebGL enabled (for Three.js logo).

---

## 2. Fast-Track Deployment (Docker Compose)

The recommended way to deploy Humbug is via Docker Compose.

### Step 1: Clone Repository
```bash
git clone https://github.com/Pooja-Ramdas/Humbug.git
cd Humbug
```

### Step 2: Build and Start Containers
```bash
docker compose up --build -d
```

### Step 3: Access Applications
- **Operator Console UI**: Open **`http://localhost`**
- **Backend API**: Open **`http://localhost:8000/health`**

### Step 4: Verify Deployment
Run the following curl command to verify backend health:
```bash
curl http://localhost:8000/health
```

Expected JSON response:
```json
{
  "status": "ok",
  "pole_count": 2750,
  "open_tickets": 0,
  "connected": true
}
```

---

## 3. Standalone Development Setup (Without Docker)

### Backend Setup (Python 3.11)

1. Navigate to repository root:
   ```bash
   cd Humbug
   ```

2. Create and activate a Python virtual environment:
   ```bash
   # Windows (PowerShell)
   python -m venv venv
   .\venv\Scripts\Activate.ps1

   # Linux / macOS
   python3 -m venv venv
   source venv/bin/activate
   ```

3. Install required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Start standalone backend server:
   ```bash
   python server.py
   ```
   *The Flask server starts on `http://localhost:8000` and initializes `data/data.db`.*

### Frontend Setup

The frontend is built with zero-build static HTML/JS (Leaflet + React 18 UMD). No `npm install` or `npm run build` is required.

To serve the frontend static files:

```bash
# In a new terminal window
cd Humbug/frontend
python -m http.server 8080
```

Open **`http://localhost:8080`** in your browser.

---

## 4. Environment Configuration (`.env`)

Copy `.env.example` to `.env` in the repository root to override defaults:

```bash
cp .env.example .env
```

### Available Variables

| Variable | Default | Description |
| :--- | :--- | :--- |
| `DB_PATH` | `/app/data/data.db` | Absolute path to SQLite database file. |
| `GENERATE_SEED` | `42` | Seed for synthetic dataset generator (`generate_data.py`). |
| `HOST` | `0.0.0.0` | Host interface for server binding. |
| `PORT` | `8000` | Port for backend API server. |
| `HUMBUG_API_BASE` | `http://localhost:8000` | Frontend API base URL (when running outside Nginx). |

---

## 5. Troubleshooting Guide

### Issue 1: Port Conflicts (Port 80 or 8000 in use)
**Symptom**: `Error starting userland proxy: listen tcp4 0.0.0.0:80: bind: address already in use`.

**Fix**:
1. Identify process using port 80 or 8000:
   ```bash
   # Windows
   netstat -ano | findstr :80
   # Linux / macOS
   lsof -i :80
   ```
2. Modify port mappings in `docker-compose.yml`:
   ```yaml
   ports:
     - "8080:80"   # Change frontend external port to 8080
   ```

---

### Issue 2: SQLite File Locking (`database is locked`)
**Symptom**: `sqlite3.OperationalError: database is locked`.

**Fix**:
1. Ensure WAL mode is active:
   ```sql
   PRAGMA journal_mode=WAL;
   ```
2. If running locally, check if multiple `server.py` or python instances are holding lock files (`data.db-wal` or `data.db-shm`). Stop extra instances.

---

### Issue 3: CORS (Cross-Origin Resource Sharing) Errors
**Symptom**: Browser console error: `Access to fetch at 'http://localhost:8000' from origin 'http://localhost:8080' has been blocked by CORS policy`.

**Fix**:
- Both `backend/app.py` and `server.py` include `CORSMiddleware` with `allow_origins=["*"]`.
- When using Docker Compose, the Nginx container proxies `/api/*` directly to `backend:8000`, bypassing CORS completely.

---

### Issue 4: Browser Caching & Stale Map Markers
**Symptom**: Map styles or ticket drawer updates do not reflect latest backend changes.

**Fix**:
- Force browser hard reload: `Ctrl + F5` (Windows) or `Cmd + Shift + R` (macOS).
- Open Developer Tools -> Network tab -> Check "Disable cache".

---

## 6. Resetting System to Clean Baseline State

To wipe all active faults, telemetry history, and generated tickets, and restore pristine baseline data:

### Method A: Using Python Script (Fastest)
```bash
python reset_test_db.py
```

### Method B: Re-generating Synthetic Data
```bash
python generate_data.py
```

### Method C: Resetting Docker Volumes
```bash
docker compose down -v
docker compose up --build
```
