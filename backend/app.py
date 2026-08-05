"""
backend/app.py — Humbug Fault Localization API

FastAPI application. Exposes all endpoints needed by the operator console:
  - Telemetry ingest
  - Pole/network data
  - Ticket lifecycle
  - Fault simulator
  - Scheduled outages
  - System health

On startup: initialises the runtime tables (telemetry, tickets, etc.) in the
existing data.db, loads the network graph, and seeds initial heartbeat state
so the map shows live poles immediately.
"""

import os
import sys
import math
import time
import uuid
import sqlite3
import logging
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional, List

import networkx as nx
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Add repo root to path so we can import detection, build_graph, telemetry_sim
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from detection import run_detection_pass, compute_pole_statuses
from build_graph import build_graph
import telemetry_sim as sim

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("DB_PATH", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "data.db"
))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("humbug")

# Global graph — rebuilt on startup, never replaced at runtime
_graph: Optional[nx.DiGraph] = None

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con

def init_runtime_tables(con: sqlite3.Connection):
    """Create tables that don't exist in the seed DB but are needed at runtime."""
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS telemetry (
            id          TEXT PRIMARY KEY,
            pole_id     TEXT NOT NULL,
            device_id   TEXT,
            event       TEXT NOT NULL,
            energized   INTEGER,
            ts          REAL,
            seq         INTEGER,
            battery_mv  INTEGER,
            rssi        INTEGER,
            fw          TEXT,
            received_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_tel_pole ON telemetry(pole_id);
        CREATE INDEX IF NOT EXISTS idx_tel_device ON telemetry(device_id);
        CREATE INDEX IF NOT EXISTS idx_tel_ts ON telemetry(ts);

        CREATE TABLE IF NOT EXISTS device_last_seen (
            device_id TEXT PRIMARY KEY,
            ts        REAL NOT NULL,
            pole_id   TEXT
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id                TEXT PRIMARY KEY,
            scope             TEXT NOT NULL,
            target_id         TEXT NOT NULL,
            affected_pole_ids TEXT,
            status            TEXT NOT NULL DEFAULT 'detected',
            confidence        REAL,
            confidence_reason TEXT,
            lat               REAL,
            lon               REAL,
            pincode           TEXT,
            feeder_id         TEXT,
            created_at        REAL,
            updated_at        REAL,
            acknowledged_at   REAL,
            crew_assigned_at  REAL,
            resolved_at       REAL,
            verified_at       REAL,
            closed_at         REAL
        );
        CREATE INDEX IF NOT EXISTS idx_tkt_status ON tickets(status);
        CREATE INDEX IF NOT EXISTS idx_tkt_target ON tickets(target_id);

        CREATE TABLE IF NOT EXISTS scheduled_outages (
            id         TEXT PRIMARY KEY,
            scope      TEXT NOT NULL,
            target_id  TEXT NOT NULL,
            start_ts   REAL NOT NULL,
            end_ts     REAL NOT NULL,
            status     TEXT NOT NULL DEFAULT 'active',
            reason     TEXT
        );

        CREATE TABLE IF NOT EXISTS active_faults (
            id          TEXT PRIMARY KEY,
            fault_type  TEXT NOT NULL,
            target_id   TEXT NOT NULL,
            injected_at REAL NOT NULL,
            restored    INTEGER DEFAULT 0
        );
    """)
    try:
        cur.execute("ALTER TABLE scheduled_outages ADD COLUMN status TEXT DEFAULT 'active'")
    except sqlite3.OperationalError:
        pass
    con.commit()


def seed_heartbeats(con: sqlite3.Connection):
    """
    Seed or refresh device_last_seen entries for all devices so that poles show
    as 'normal' on first load rather than all appearing stale/fault.
    Updates all healthy devices to recently seen, while preserving staleness
    for devices that have active, unrestored faults.
    """
    cur = con.cursor()
    now = time.time()

    # 1. Identify all devices currently affected by active (unrestored) faults
    try:
        active = cur.execute("SELECT fault_type, target_id FROM active_faults WHERE restored = 0").fetchall()
    except sqlite3.OperationalError:
        # active_faults table might not exist yet if called too early, but init_runtime_tables ran first
        active = []

    stale_devices = set()
    for row in active:
        # Use our telemetry simulator's helper to get affected poles
        affected = sim.get_poles_for_target(con, row["fault_type"], row["target_id"], _graph)
        for pole in affected:
            if pole.get("device_id"):
                stale_devices.add(pole["device_id"])

    rows = cur.execute(
        "SELECT device_id, pole_id FROM pole_registry WHERE device_id IS NOT NULL"
    ).fetchall()

    # 2. Update or insert device_last_seen
    for r in rows:
        device_id = r["device_id"]
        pole_id = r["pole_id"]
        if device_id in stale_devices:
            # Stale device under active fault: insert as stale if not already exists,
            # or leave it alone if it exists (to preserve the exact simulated fault timestamp).
            exists = cur.execute("SELECT 1 FROM device_last_seen WHERE device_id = ?", (device_id,)).fetchone()
            if not exists:
                stale_ts = now - sim.STALE_THRESHOLD_S - 60
                cur.execute(
                    "INSERT INTO device_last_seen (device_id, ts, pole_id) VALUES (?, ?, ?)",
                    (device_id, stale_ts, pole_id)
                )
        else:
            # Healthy device: set to now - 60 (or update if already exists)
            cur.execute(
                "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) VALUES (?, ?, ?)",
                (device_id, now - 60, pole_id)
            )

    con.commit()
    log.info("Synchronized device heartbeats on startup.")


# ---------------------------------------------------------------------------
# Topology inference helpers (for the 60% of DTs with no recorded topology)
# ---------------------------------------------------------------------------

def infer_topology_mst(con: sqlite3.Connection) -> List[dict]:
    """
    For DTs missing pole ordering, build a geometric MST rooted at the DT
    to infer likely line order. Returns a list of inferred edges:
      {from_id, to_id, dt_id, edge_type='inferred', weight_m}
    These are NOT written to pole_registry — they are returned for the
    frontend map only and clearly labelled as inferred, not surveyed.
    """
    cur = con.cursor()
    inferred_edges = []

    # Find DTs that have membership edges in graph (topology unknown)
    dt_rows = cur.execute(
        "SELECT DISTINCT dt_id, lat, lon FROM transformer_registry"
    ).fetchall()

    for dt_row in dt_rows:
        dt_id = dt_row["dt_id"]
        dt_lat, dt_lon = dt_row["lat"], dt_row["lon"]

        # Check if this DT has topology
        has_topo = cur.execute(
            "SELECT COUNT(*) FROM pole_registry "
            "WHERE dt_id=? AND seq_on_line IS NOT NULL", (dt_id,)
        ).fetchone()[0]
        if has_topo > 0:
            continue  # Real topology exists — skip inference

        poles = cur.execute(
            "SELECT pole_id, lat, lon FROM pole_registry WHERE dt_id=?", (dt_id,)
        ).fetchall()
        if len(poles) < 2:
            continue

        # Build points: DT + all poles
        nodes = [("__DT__", dt_lat, dt_lon)] + [(r["pole_id"], r["lat"], r["lon"]) for r in poles]

        # Prim's MST with haversine distance
        in_tree = {nodes[0][0]}
        edges_left = list(nodes[1:])

        while edges_left:
            best_dist = float("inf")
            best_edge = None
            best_from = None
            for candidate in edges_left:
                for tree_node in nodes:
                    if tree_node[0] not in in_tree:
                        continue
                    d = _haversine(tree_node[1], tree_node[2], candidate[1], candidate[2])
                    if d < best_dist:
                        best_dist = d
                        best_edge = candidate
                        best_from = tree_node[0]

            if best_edge is None:
                break

            from_id = best_from if best_from != "__DT__" else dt_id
            inferred_edges.append({
                "from_id": from_id,
                "to_id": best_edge[0],
                "dt_id": dt_id,
                "edge_type": "inferred",
                "weight_m": best_dist,
            })
            in_tree.add(best_edge[0])
            edges_left = [e for e in edges_left if e[0] not in in_tree]

    return inferred_edges


def _haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dlmb/2)**2
    return 2 * R * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Confidence and localization helpers
# ---------------------------------------------------------------------------

def compute_ticket_metadata(con: sqlite3.Connection, dt_id: str,
                             dark_poles: List[str]) -> dict:
    """
    Compute lat/lon, pincode, confidence and confidence_reason for a ticket.
    lat/lon = centroid of dark poles (reasonable nav target for crew).
    Confidence is a simple heuristic:
      - HIGH (0.9): ≥2 dark poles with known topology, clear live/dark boundary
      - MEDIUM (0.65): topology unknown, ≥2 dark poles
      - LOW (0.4): single dark pole or all poles in DT dark (ambiguous)
    """
    cur = con.cursor()
    if not dark_poles:
        return {"lat": None, "lon": None, "pincode": None,
                "confidence": 0.3, "confidence_reason": "No dark poles found"}

    placeholders = ",".join("?" * len(dark_poles))
    pole_rows = cur.execute(
        f"SELECT lat, lon, pincode, seq_on_line FROM pole_registry "
        f"WHERE pole_id IN ({placeholders})", dark_poles
    ).fetchall()

    lats = [r["lat"] for r in pole_rows]
    lons = [r["lon"] for r in pole_rows]
    pincodes = [r["pincode"] for r in pole_rows if r["pincode"]]
    has_topo = any(r["seq_on_line"] is not None for r in pole_rows)

    lat = sum(lats) / len(lats) if lats else None
    lon = sum(lons) / len(lons) if lons else None
    pincode = max(set(pincodes), key=pincodes.count) if pincodes else _fallback_pincode(con, dt_id)

    total_poles = cur.execute(
        "SELECT COUNT(*) FROM pole_registry WHERE dt_id=?", (dt_id,)
    ).fetchone()[0]

    n_dark = len(dark_poles)
    all_dark = (n_dark >= total_poles * 0.95)

    if all_dark:
        confidence = 0.55
        reason = f"Entire DT dark ({n_dark}/{total_poles} poles) — likely DT-level fault or feeder issue"
    elif has_topo and n_dark >= 2:
        confidence = 0.90
        reason = f"Clear live/dark boundary on known topology ({n_dark} poles dark)"
    elif n_dark >= 2:
        confidence = 0.65
        reason = f"{n_dark} poles dark, topology unknown for this DT — DT-level localization only"
    else:
        confidence = 0.40
        reason = "Single dark pole — could be isolated device failure"

    return {"lat": lat, "lon": lon, "pincode": pincode,
            "confidence": confidence, "confidence_reason": reason}


def _fallback_pincode(con, dt_id: str) -> Optional[str]:
    """Borrow the most common pincode from sibling poles on the same DT."""
    row = con.execute(
        "SELECT pincode FROM pole_registry WHERE dt_id=? AND pincode IS NOT NULL "
        "GROUP BY pincode ORDER BY COUNT(*) DESC LIMIT 1", (dt_id,)
    ).fetchone()
    return row["pincode"] if row else None

# ---------------------------------------------------------------------------
# Enhanced detection pass (wraps detection.py, adds confidence + metadata)
# ---------------------------------------------------------------------------

def run_full_detection(con: sqlite3.Connection):
    """
    Run detection, then enrich newly-opened tickets with confidence metadata,
    and check for scheduled outages to suppress false positives.
    """
    global _graph
    statuses = run_detection_pass(con, _graph)

    # Enrich tickets that are missing metadata
    cur = con.cursor()
    needs_meta = cur.execute(
        "SELECT id, target_id, affected_pole_ids FROM tickets "
        "WHERE status NOT IN ('verified','closed') AND confidence IS NULL"
    ).fetchall()

    now = time.time()
    for ticket in needs_meta:
        t_id, dt_id, affected_csv = ticket["id"], ticket["target_id"], ticket["affected_pole_ids"]
        dark_poles = affected_csv.split(",") if affected_csv else []
        meta = compute_ticket_metadata(con, dt_id, dark_poles)

        # Check if this DT/feeder is under a scheduled outage, or if all dark poles are suppressed
        feeder_row = cur.execute(
            "SELECT feeder_id FROM transformer_registry WHERE dt_id=?", (dt_id,)
        ).fetchone()
        feeder_id = feeder_row["feeder_id"] if feeder_row else None

        is_dt_or_feeder_scheduled = cur.execute(
            "SELECT id FROM scheduled_outages WHERE start_ts <= ? AND end_ts >= ? "
            "AND (target_id=? OR target_id=?)",
            (now, now, dt_id, feeder_id or "")
        ).fetchone()

        all_suppressed = False
        if is_dt_or_feeder_scheduled:
            all_suppressed = True
        elif dark_poles:
            all_suppressed = True
            for pid in dark_poles:
                pole_row = cur.execute(
                    "SELECT dt_id, feeder_id FROM pole_registry WHERE pole_id=?", (pid,)
                ).fetchone()
                p_dt_id = pole_row["dt_id"] if pole_row else None
                p_feeder_id = pole_row["feeder_id"] if pole_row else None
                is_pol_sched = cur.execute(
                    "SELECT id FROM scheduled_outages WHERE status='active' AND start_ts <= ? AND end_ts >= ? "
                    "AND (target_id=? OR target_id=? OR target_id=?)",
                    (now, now, pid, p_dt_id or "", p_feeder_id or "")
                ).fetchone()
                if not is_pol_sched:
                    all_suppressed = False
                    break

        if all_suppressed:
            # Suppress: mark as load_shedding (not a real fault ticket)
            cur.execute(
                "UPDATE tickets SET status='closed', confidence=0.1, "
                "confidence_reason='Matches active scheduled outage window — suppressed', "
                "updated_at=? WHERE id=?", (now, t_id)
            )
        else:
            cur.execute(
                "UPDATE tickets SET confidence=?, confidence_reason=?, "
                "lat=?, lon=?, pincode=?, feeder_id=?, updated_at=? WHERE id=?",
                (meta["confidence"], meta["confidence_reason"],
                 meta["lat"], meta["lon"], meta["pincode"], feeder_id, now, t_id)
            )

    # detection.py sets status='verified' but doesn't set verified_at.
    # Stamp it here for any newly-verified tickets.
    now2 = time.time()
    cur.execute(
        "UPDATE tickets SET verified_at=?, updated_at=? "
        "WHERE status='verified' AND verified_at IS NULL",
        (now2, now2)
    )
    # Auto-close tickets that have been verified after a 10-second grace period
    cur.execute(
        "UPDATE tickets SET status='closed', closed_at=?, updated_at=? "
        "WHERE status='verified' AND verified_at IS NOT NULL AND (? - verified_at) >= 10 AND closed_at IS NULL",
        (now2, now2, now2)
    )
    con.commit()
    return statuses


# ---------------------------------------------------------------------------
# Lifespan — startup/shutdown
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _graph
    log.info(f"Starting Humbug backend. DB: {DB_PATH}")
    _graph = build_graph(DB_PATH)
    log.info(f"Graph loaded: {_graph.number_of_nodes()} nodes, {_graph.number_of_edges()} edges")
    con = get_db()
    init_runtime_tables(con)
    seed_heartbeats(con)
    con.close()

    # ── Background heartbeat thread ──────────────────────────────────────────
    # Real pole IoT devices send a heartbeat every ~15 min forever.
    # This simulation has no ongoing telemetry stream, so without this thread
    # every device's device_last_seen would age past STALE_THRESHOLD_S
    # (currently 60s) exactly once — at startup+60s — flipping the entire fleet
    # to fault simultaneously. That defeats the correlated-staleness safeguard
    # (which requires >=2 peers stale at once to call it a line fault, not a
    # dead modem), because when every device goes stale at the same instant the
    # safeguard is trivially satisfied everywhere.
    _hb_stop = threading.Event()

    def heartbeat_refresh():
        """
        Update device_last_seen to now() for all devices that are healthy.

        A device is excluded from refresh (kept stale) if:
          (a) Its most-recent telemetry event is 'power_lost' — it actively
              signalled it is dark. This covers fw>=1.3 devices during a fault.
          (b) It is the device of a pole under an active, unrestored injected
              fault — this covers fw-1.2 silent devices (which go stale but
              never emit power_lost) and the 30%-lost dying-message cases.

        Every other device is presumed healthy and gets ts=now, simulating
        the continuous heartbeat a real IoT device sends every ~15 min.
        """
        try:
            hb_con = get_db()
            cur = hb_con.cursor()
            now = time.time()
            cutoff = now - 172800  # 48h — bound telemetry scan

            # --- Exclusion group A: device's latest telemetry is power_lost ---
            dark_by_telemetry = set(
                r[0] for r in cur.execute(
                    """
                    SELECT t1.device_id
                    FROM   telemetry t1
                    INNER JOIN (
                        SELECT device_id, MAX(ts) AS max_ts
                        FROM   telemetry
                        WHERE  ts >= ?
                        GROUP  BY device_id
                    ) t2 ON t1.device_id = t2.device_id AND t1.ts = t2.max_ts
                    WHERE  t1.event = 'power_lost'
                    """,
                    (cutoff,)
                ).fetchall()
            )

            # --- Exclusion group B: device belongs to a pole under an active fault ---
            # This covers fw-1.2 silent and dying-message-lost cases where
            # device_last_seen was backdated but power_lost was never written.
            dark_by_fault = set()
            try:
                active_fault_rows = cur.execute(
                    "SELECT fault_type, target_id FROM active_faults WHERE restored = 0"
                ).fetchall()
                for row in active_fault_rows:
                    affected = sim.get_poles_for_target(
                        hb_con, row["fault_type"], row["target_id"], _graph
                    )
                    for pole in affected:
                        if pole.get("device_id"):
                            dark_by_fault.add(pole["device_id"])
            except sqlite3.OperationalError:
                pass  # active_faults table may not exist in test DB

            excluded = dark_by_telemetry | dark_by_fault

            # --- Refresh all healthy devices ---
            all_devices = cur.execute(
                "SELECT device_id, pole_id FROM pole_registry "
                "WHERE device_id IS NOT NULL"
            ).fetchall()

            refreshed = 0
            for r in all_devices:
                if r["device_id"] not in excluded:
                    cur.execute(
                        "INSERT OR REPLACE INTO device_last_seen "
                        "(device_id, ts, pole_id) VALUES (?, ?, ?)",
                        (r["device_id"], now, r["pole_id"])
                    )
                    refreshed += 1

            hb_con.commit()
            hb_con.close()
            log.debug(
                f"[heartbeat] refreshed={refreshed}, "
                f"excluded={len(excluded)} "
                f"(dark_telemetry={len(dark_by_telemetry)}, "
                f"(dark_fault={len(dark_by_fault)})"
            )
        except Exception as exc:
            log.warning(f"[heartbeat] refresh error: {exc}")

    # Run at half the stale threshold so there's always headroom before devices age out.
    # With STALE_THRESHOLD_S=60s this fires every 20s. With a production value of
    # 30min it fires every 10min — lightweight either way.
    from telemetry_sim import STALE_THRESHOLD_S as _STH
    HEARTBEAT_INTERVAL_S = max(10, _STH // 3)
    log.info(f"[heartbeat] Starting background refresh thread "
             f"(interval={HEARTBEAT_INTERVAL_S}s, stale_threshold={_STH}s)")

    def heartbeat_loop():
        # Fire immediately once at startup (in addition to seed_heartbeats)
        # so the first poll always sees fresh timestamps even if uvicorn
        # takes a few seconds to load.
        heartbeat_refresh()
        while not _hb_stop.wait(HEARTBEAT_INTERVAL_S):
            heartbeat_refresh()

    _hb_thread = threading.Thread(target=heartbeat_loop, daemon=True, name="hb-refresh")
    _hb_thread.start()

    yield

    # Signal the heartbeat thread to stop and give it a moment to exit cleanly.
    _hb_stop.set()
    _hb_thread.join(timeout=5)
    log.info("Humbug backend shutting down")




# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Humbug", version="1.0.0", lifespan=lifespan)

BASE_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = BASE_DIR / "frontend"

if (FRONTEND_DIR / "css").exists():
    app.mount("/css", StaticFiles(directory=str(FRONTEND_DIR / "css")), name="css")

if (FRONTEND_DIR / "js").exists():
    app.mount("/js", StaticFiles(directory=str(FRONTEND_DIR / "js")), name="js")

if (FRONTEND_DIR / "fonts").exists():
    app.mount("/fonts", StaticFiles(directory=str(FRONTEND_DIR / "fonts")), name="fonts")

if (FRONTEND_DIR / "public").exists():
    app.mount("/public", StaticFiles(directory=str(FRONTEND_DIR / "public")), name="public")


@app.get("/")
async def index():
    if (FRONTEND_DIR / "index.html").exists():
        return FileResponse(FRONTEND_DIR / "index.html")
    return {"status": "Humbug backend is running"}


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class TelemetryPayload(BaseModel):
    device_id: str
    pole_id: str
    event: str
    energized: bool
    ts: float
    seq: int
    battery_mv: Optional[int] = None
    rssi: Optional[int] = None
    fw: Optional[str] = None


class TicketActionBody(BaseModel):
    note: Optional[str] = None


class SimulateFaultBody(BaseModel):
    type: str          # span | dt | feeder
    target_id: str


class SimulateRestoreBody(BaseModel):
    type: str
    target_id: str


class SimulateLoadShedBody(BaseModel):
    target_id: str
    scope: str
    duration_minutes: int
    start_delay_minutes: Optional[int] = 0


class SimulateNoiseBody(BaseModel):
    noise_type: str    # device_death | load_shed | duplicate_burst
    target_id: Optional[str] = None
    scope: Optional[str] = None
    duration_minutes: Optional[int] = 60


class ScheduledOutageBody(BaseModel):
    scope: str
    target_id: str
    start_ts: float
    end_ts: float
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# Health / stats
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    con = get_db()
    try:
        pole_count = con.execute("SELECT COUNT(*) FROM pole_registry").fetchone()[0]
        open_tickets = con.execute(
            "SELECT COUNT(*) FROM tickets WHERE status NOT IN ('verified','closed')"
        ).fetchone()[0]
        return {"status": "ok", "pole_count": pole_count, "open_tickets": open_tickets}
    finally:
        con.close()


@app.get("/stats")
def stats():
    con = get_db()
    try:
        pole_count   = con.execute("SELECT COUNT(*) FROM pole_registry").fetchone()[0]
        open_tickets = con.execute(
            "SELECT COUNT(*) FROM tickets WHERE status NOT IN ('verified','closed')"
        ).fetchone()[0]
        dt_count     = con.execute("SELECT COUNT(*) FROM transformer_registry").fetchone()[0]
        feeder_count = con.execute("SELECT COUNT(*) FROM feeders").fetchone()[0]

        # Count poles actually in 'fault' status right now (derived, not from tickets).
        # This is the number that matches the red dots on the map.
        pole_rows = con.execute("SELECT pole_id, device_id FROM pole_registry").fetchall()
        statuses = compute_pole_statuses(con, [(r["pole_id"], r["device_id"]) for r in pole_rows])
        fault_pole_count    = sum(1 for s in statuses.values() if s == "fault")
        load_shed_pole_count = sum(1 for s in statuses.values() if s == "load_shed")
        device_fault_count  = sum(1 for s in statuses.values() if s == "device_fault")

        return {
            "pole_count":         pole_count,
            "dt_count":           dt_count,
            "feeder_count":       feeder_count,
            "open_tickets":       open_tickets,
            "fault_pole_count":   fault_pole_count,    # red poles on map
            "load_shed_pole_count": load_shed_pole_count,  # yellow poles
            "device_fault_count": device_fault_count,  # grey (IoT issue) poles
        }
    finally:
        con.close()

# ---------------------------------------------------------------------------
# Telemetry ingest
# ---------------------------------------------------------------------------

@app.post("/ingest")
def ingest(payload: TelemetryPayload):
    """
    Accept a single telemetry event from a pole device.
    Dedup by (pole_id, seq) — duplicate messages are silently accepted
    (200 OK) but not double-processed.
    Triggers a detection pass after every write.
    """
    con = get_db()
    try:
        cur = con.cursor()

        # Dedup: if we already have this (pole_id, seq), skip insertion
        existing = cur.execute(
            "SELECT id FROM telemetry WHERE pole_id=? AND seq=?",
            (payload.pole_id, payload.seq)
        ).fetchone()

        now = time.time()
        event_id = str(uuid.uuid4())

        if not existing:
            cur.execute(
                "INSERT INTO telemetry (id, pole_id, device_id, event, energized, "
                "ts, seq, battery_mv, rssi, fw, received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, payload.pole_id, payload.device_id, payload.event,
                 int(payload.energized), payload.ts, payload.seq,
                 payload.battery_mv, payload.rssi, payload.fw, now)
            )
            # Update device_last_seen for staleness detection
            cur.execute(
                "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) VALUES (?,?,?)",
                (payload.device_id, payload.ts, payload.pole_id)
            )
            con.commit()

        run_full_detection(con)
        return {"status": "ok", "id": event_id, "duplicate": existing is not None}
    finally:
        con.close()


@app.post("/ingest/batch")
def ingest_batch(payloads: List[TelemetryPayload]):
    """Batch ingest for burst scenarios."""
    con = get_db()
    try:
        cur = con.cursor()
        accepted = 0
        for payload in payloads:
            existing = cur.execute(
                "SELECT id FROM telemetry WHERE pole_id=? AND seq=?",
                (payload.pole_id, payload.seq)
            ).fetchone()
            if not existing:
                now = time.time()
                cur.execute(
                    "INSERT INTO telemetry (id, pole_id, device_id, event, energized, "
                    "ts, seq, battery_mv, rssi, fw, received_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(uuid.uuid4()), payload.pole_id, payload.device_id, payload.event,
                     int(payload.energized), payload.ts, payload.seq,
                     payload.battery_mv, payload.rssi, payload.fw, now)
                )
                cur.execute(
                    "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) VALUES (?,?,?)",
                    (payload.device_id, payload.ts, payload.pole_id)
                )
                accepted += 1
        con.commit()
        run_full_detection(con)
        return {"status": "ok", "accepted": accepted, "total": len(payloads)}
    finally:
        con.close()

# ---------------------------------------------------------------------------
# Poles and network data
# ---------------------------------------------------------------------------

@app.get("/poles")
def get_poles():
    """
    All poles with current status. Status is derived fresh from DB on every call.
    status values: normal | fault | unknown | load_shedding
    """
    con = get_db()
    try:
        global _graph
        cur = con.cursor()

        pole_rows = cur.execute(
            "SELECT pole_id, device_id FROM pole_registry"
        ).fetchall()
        statuses = compute_pole_statuses(con, [(r["pole_id"], r["device_id"]) for r in pole_rows])

        # Which DTs/feeders are in a scheduled outage right now?
        # NOTE: pole-level load shedding is not physically meaningful — excluded.
        now = time.time()
        outage_targets = set()
        outages = cur.execute(
            "SELECT scope, target_id FROM scheduled_outages "
            "WHERE start_ts <= ? AND end_ts >= ? AND scope IN ('feeder','dt')", (now, now)
        ).fetchall()
        for o in outages:
            outage_targets.add((o["scope"], o["target_id"]))


        # Device last seen for tooltip
        last_seen_map = {
            r["device_id"]: r["ts"]
            for r in cur.execute("SELECT device_id, ts FROM device_last_seen").fetchall()
        }

        all_poles = cur.execute(
            "SELECT pole_id, lat, lon, feeder_id, dt_id, seq_on_line, "
            "parent_pole_id, pole_type, ward, pincode, device_id "
            "FROM pole_registry"
        ).fetchall()

        result = []
        for p in all_poles:
            pid = p["pole_id"]
            raw_status = statuses.get(pid, "unknown")

            # Override: if pole is under a feeder/DT scheduled outage, show load_shed.
            # NOTE: pole-level load shedding does not exist in practice — load shedding
            # is always feeder or DT level. detection.py already handles this correctly;
            # this override is a belt-and-suspenders guard for direct DB queries.
            final_status = raw_status
            dt_id = p["dt_id"]
            feeder_id = p["feeder_id"]
            if (("feeder", feeder_id) in outage_targets or ("dt", dt_id) in outage_targets):
                final_status = "load_shed"

            device_id = p["device_id"]
            last_seen = last_seen_map.get(device_id) if device_id else None

            result.append({
                "pole_id": pid,
                "lat": p["lat"],
                "lon": p["lon"],
                "feeder_id": p["feeder_id"],
                "dt_id": p["dt_id"],
                "seq_on_line": p["seq_on_line"],
                "parent_pole_id": p["parent_pole_id"],
                "pole_type": p["pole_type"],
                "ward": p["ward"],
                "pincode": p["pincode"],
                "device_id": device_id,
                "has_device": device_id is not None,
                "status": final_status,
                "last_seen": last_seen,
            })

        return result
    finally:
        con.close()


@app.get("/poles/{pole_id}")
def get_pole(pole_id: str):
    con = get_db()
    try:
        row = con.execute(
            "SELECT * FROM pole_registry WHERE pole_id=?", (pole_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, f"Pole {pole_id} not found")
        pole_rows = [(pole_id, row["device_id"])]
        statuses = compute_pole_statuses(con, pole_rows)
        d = dict(row)
        d["status"] = statuses.get(pole_id, "unknown")
        return d
    finally:
        con.close()


@app.get("/network/topology")
def get_network_topology():
    """
    Static topology data — pole positions, DT/feeder/pincode/ward/device
    attributes. This NEVER changes at runtime. Load once on page init and
    cache client-side; do NOT poll this endpoint.
    """
    con = get_db()
    try:
        poles = con.execute(
            "SELECT pole_id, lat, lon, feeder_id, dt_id, seq_on_line, "
            "parent_pole_id, pole_type, ward, pincode, device_id "
            "FROM pole_registry"
        ).fetchall()
        return [dict(r) for r in poles]
    finally:
        con.close()


@app.get("/network/status")
def get_network_status():
    """
    Lightweight status-only response: {pole_id: status} for all poles.
    This is the ONLY endpoint that should be polled frequently (every 3s).
    The frontend can update just marker colors without recreating any DOM nodes.
    """
    con = get_db()
    try:
        pole_rows = con.execute(
            "SELECT pole_id, device_id FROM pole_registry"
        ).fetchall()
        statuses = compute_pole_statuses(con, [(r["pole_id"], r["device_id"]) for r in pole_rows])
        return statuses
    finally:
        con.close()


@app.get("/network/edges")
def get_network_edges():
    """
    Return topology edges for the map:
    - Real span edges (from pole_registry parent_pole_id)
    - Inferred MST edges for topology-unknown DTs (labelled as inferred)
    Does NOT include DT->feeder->substation hierarchy edges.
    """
    con = get_db()
    try:
        cur = con.cursor()
        edges = []

        # Real span edges
        span_poles = cur.execute(
            "SELECT pole_id, parent_pole_id, dt_id FROM pole_registry "
            "WHERE parent_pole_id IS NOT NULL"
        ).fetchall()
        for p in span_poles:
            edges.append({
                "from_id": p["parent_pole_id"],
                "to_id": p["pole_id"],
                "dt_id": p["dt_id"],
                "edge_type": "span",
            })

        # DT root connections (first pole on each branch: parent_pole_id IS NULL but seq IS NOT NULL)
        dt_roots = cur.execute(
            "SELECT p.pole_id, p.dt_id, t.lat as dt_lat, t.lon as dt_lon "
            "FROM pole_registry p JOIN transformer_registry t ON p.dt_id=t.dt_id "
            "WHERE p.parent_pole_id IS NULL AND p.seq_on_line IS NOT NULL"
        ).fetchall()
        for p in dt_roots:
            edges.append({
                "from_id": p["dt_id"],
                "to_id": p["pole_id"],
                "dt_id": p["dt_id"],
                "edge_type": "span",
            })

        # Inferred MST edges for topology-unknown DTs
        inferred = infer_topology_mst(con)
        edges.extend(inferred)

        return edges
    finally:
        con.close()


@app.get("/transformers")
def get_transformers():
    con = get_db()
    try:
        rows = con.execute(
            "SELECT dt_id, feeder_id, lat, lon, capacity_kva, households_served "
            "FROM transformer_registry"
        ).fetchall()
        result = []
        for r in rows:
            has_topo = con.execute(
                "SELECT COUNT(*) FROM pole_registry WHERE dt_id=? AND seq_on_line IS NOT NULL",
                (r["dt_id"],)
            ).fetchone()[0] > 0
            d = dict(r)
            d["topology_known"] = has_topo
            result.append(d)
        return result
    finally:
        con.close()


@app.get("/feeders")
def get_feeders():
    con = get_db()
    try:
        rows = con.execute("SELECT * FROM feeders").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@app.get("/substations")
def get_substations():
    con = get_db()
    try:
        rows = con.execute("SELECT * FROM substations").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()

# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

def _ticket_row_to_dict(row) -> dict:
    d = dict(row)
    # Parse affected_pole_ids CSV into a list
    csv_val = d.get("affected_pole_ids") or ""
    d["affected_poles"] = [p for p in csv_val.split(",") if p]
    d["affected_pole_count"] = len(d["affected_poles"])
    return d


@app.get("/tickets")
def get_tickets(status: Optional[str] = Query(None)):
    """
    List tickets. ?status=open returns all non-closed/non-verified.
    Default returns all tickets ordered by recency.
    """
    con = get_db()
    try:
        if status == "open":
            rows = con.execute(
                "SELECT * FROM tickets WHERE status NOT IN ('verified','closed') "
                "ORDER BY created_at DESC"
            ).fetchall()
        elif status:
            rows = con.execute(
                "SELECT * FROM tickets WHERE status=? ORDER BY created_at DESC",
                (status,)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM tickets ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        return [_ticket_row_to_dict(r) for r in rows]
    finally:
        con.close()


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    con = get_db()
    try:
        row = con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
        if not row:
            raise HTTPException(404, f"Ticket {ticket_id} not found")
        return _ticket_row_to_dict(row)
    finally:
        con.close()


def _advance_ticket(con, ticket_id: str, required_status: str,
                    new_status: str, ts_field: str):
    """Generic ticket state transition with guard."""
    cur = con.cursor()
    row = cur.execute("SELECT status FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"Ticket {ticket_id} not found")
    if row["status"] != required_status:
        raise HTTPException(409,
            f"Cannot transition to '{new_status}': ticket is '{row['status']}', "
            f"expected '{required_status}'")
    now = time.time()
    cur.execute(
        f"UPDATE tickets SET status=?, {ts_field}=?, updated_at=? WHERE id=?",
        (new_status, now, now, ticket_id)
    )
    con.commit()
    row = con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    return _ticket_row_to_dict(row)


@app.post("/tickets/{ticket_id}/acknowledge")
def acknowledge_ticket(ticket_id: str, body: TicketActionBody = Body(default=TicketActionBody())):
    con = get_db()
    try:
        return _advance_ticket(con, ticket_id, "detected", "acknowledged", "acknowledged_at")
    finally:
        con.close()


@app.post("/tickets/{ticket_id}/assign-crew")
def assign_crew(ticket_id: str, body: TicketActionBody = Body(default=TicketActionBody())):
    con = get_db()
    try:
        return _advance_ticket(con, ticket_id, "acknowledged", "crew_assigned", "crew_assigned_at")
    finally:
        con.close()


@app.post("/tickets/{ticket_id}/resolve")
def resolve_ticket(ticket_id: str, body: TicketActionBody = Body(default=TicketActionBody())):
    """
    Mark ticket as 'resolved' — crew says they've fixed it.
    The ticket will only advance to 'verified' when telemetry confirms
    poles are live again. The backend enforces this; UI cannot skip it.
    """
    con = get_db()
    try:
        return _advance_ticket(con, ticket_id, "crew_assigned", "resolved", "resolved_at")
    finally:
        con.close()


# NOTE: There is intentionally NO /tickets/{id}/verify or /tickets/{id}/close endpoint.
# Those transitions are telemetry-driven only, via run_detection_pass in detection.py.
# The UI reflects this by disabling those actions.

# ---------------------------------------------------------------------------
# Simulator endpoints
# ---------------------------------------------------------------------------

@app.post("/simulate/fault")
def simulate_fault(body: SimulateFaultBody):
    """
    Inject a simulated fault. Generates realistic power_lost telemetry
    including fw-1.2 silence and 30% message loss. Triggers detection.
    """
    valid_types = {"span", "dt", "feeder"}
    if body.type not in valid_types:
        raise HTTPException(400, f"type must be one of {valid_types}")

    con = get_db()
    try:
        result = sim.inject_fault_telemetry(con, body.type, body.target_id, _graph)
        if "error" in result:
            raise HTTPException(404, result["error"])

        # Record as active fault for the repair UI
        cur = con.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO active_faults (id, fault_type, target_id, injected_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), body.type, body.target_id, time.time())
        )
        con.commit()

        run_full_detection(con)
        return result
    finally:
        con.close()


@app.post("/simulate/restore")
def simulate_restore(body: SimulateRestoreBody):
    """
    Repair a simulated fault. Sends boot + power_restored telemetry.
    Triggers detection which will auto-verify the ticket.
    """
    con = get_db()
    try:
        result = sim.inject_restore_telemetry(con, body.type, body.target_id, _graph)
        if "error" in result:
            raise HTTPException(404, result["error"])

        # Mark active fault as restored
        con.execute(
            "UPDATE active_faults SET restored=1 WHERE target_id=? AND fault_type=? AND restored=0",
            (body.target_id, body.type)
        )
        con.commit()

        run_full_detection(con)
        return result
    finally:
        con.close()


@app.post("/simulate/noise")
def simulate_noise(body: SimulateNoiseBody):
    con = get_db()
    try:
        if body.noise_type == "device_death":
            result = sim.inject_noise_device_death(con, body.target_id)
        elif body.noise_type == "load_shed":
            scope = body.scope or "dt"
            target = body.target_id
            if not target:
                raise HTTPException(400, "target_id required for load_shed noise")
            result = sim.inject_noise_scheduled_outage(
                con, scope, target, body.duration_minutes or 60
            )
        elif body.noise_type == "duplicate_burst":
            result = sim.inject_noise_duplicate_burst(con, body.target_id)
        else:
            raise HTTPException(400, "noise_type must be device_death|load_shed|duplicate_burst")

        run_full_detection(con)
        return result
    finally:
        con.close()


@app.get("/simulate/active")
def get_active_faults():
    con = get_db()
    try:
        rows = con.execute(
            "SELECT * FROM active_faults WHERE restored=0 ORDER BY injected_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@app.post("/api/simulate-load-shed")
def simulate_load_shed(body: SimulateLoadShedBody):
    """
    Schedule a load-shedding outage window.

    IMPORTANT: We do NOT backdate device_last_seen here.
    Load shedding is a *planned, controlled* outage — the utility knows about it
    in advance. The detection logic derives load_shed status by checking the
    scheduled_outages table, NOT from telemetry staleness. Backdating last_seen
    would make the corroboration pass in compute_pole_statuses() treat siblings
    as 'fault', which is exactly the bug that caused upstream poles to turn red.

    Real-world: a feeder breaker opens, power stops flowing downstream of that
    point. Upstream poles (closer to substation) are unaffected — they are still
    energized. Only the poles that are *downstream of / under* the target
    (feeder, DT, or span) lose power, and they were expected to lose it.
    """
    con = get_db()
    try:
        outage_id = f"SO-SIM-{uuid.uuid4().hex[:8]}"
        now = time.time()
        start_delay = body.start_delay_minutes or 0
        start_ts = now + start_delay * 60
        end_ts = start_ts + body.duration_minutes * 60

        con.execute(
            "INSERT INTO scheduled_outages (id, scope, target_id, start_ts, end_ts, status, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (outage_id, body.scope, body.target_id, start_ts, end_ts, "active",
             f"Load shedding ({body.duration_minutes} mins)")
        )
        con.commit()

        # Run detection so the map immediately reflects the yellow load_shed status
        # (detection reads scheduled_outages — no telemetry manipulation needed)
        run_full_detection(con)
        con.commit()

        return {"id": outage_id, "status": "active", "end_ts": end_ts}
    finally:
        con.close()


@app.get("/api/active-load-shed")
def get_active_load_shed():
    con = get_db()
    try:
        now = time.time()
        rows = con.execute(
            "SELECT * FROM scheduled_outages WHERE status = 'active' AND start_ts <= ? AND end_ts >= ?",
            (now, now)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@app.post("/api/end-load-shed/{outage_id}")
def end_load_shed(outage_id: str):
    con = get_db()
    try:
        now = time.time()
        con.execute(
            "UPDATE scheduled_outages SET status = 'ended', end_ts = ? WHERE id = ?",
            (now, outage_id)
        )
        
        # Restore affected devices
        outage = con.execute("SELECT scope, target_id FROM scheduled_outages WHERE id = ?", (outage_id,)).fetchone()
        if outage:
            scope, target_id = outage["scope"], outage["target_id"]
            affected = sim.get_poles_for_target(con, scope, target_id, _graph)
            for pole in affected:
                d_id = pole.get("device_id")
                p_id = pole.get("pole_id")
                if d_id:
                    con.execute(
                        "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) VALUES (?, ?, ?)",
                        (d_id, now, p_id)
                      )
            con.commit()
            run_full_detection(con)
        
        con.commit()
        return {"status": "ended"}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Scheduled outages
# ---------------------------------------------------------------------------

@app.get("/scheduled-outages")
def get_scheduled_outages(
    from_ts: Optional[float] = Query(None, alias="from"),
    to_ts: Optional[float] = Query(None, alias="to"),
):
    con = get_db()
    try:
        if from_ts and to_ts:
            rows = con.execute(
                "SELECT * FROM scheduled_outages "
                "WHERE start_ts <= ? AND end_ts >= ? ORDER BY start_ts",
                (to_ts, from_ts)
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM scheduled_outages ORDER BY start_ts DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


@app.post("/scheduled-outages")
def create_scheduled_outage(body: ScheduledOutageBody):
    con = get_db()
    try:
        outage_id = f"SO-{uuid.uuid4().hex[:8]}"
        con.execute(
            "INSERT INTO scheduled_outages (id, scope, target_id, start_ts, end_ts, reason) "
            "VALUES (?,?,?,?,?,?)",
            (outage_id, body.scope, body.target_id, body.start_ts, body.end_ts, body.reason)
        )
        
        # Simulating load shedding: backdate last_seen for affected devices if currently active
        now = time.time()
        if body.start_ts <= now <= body.end_ts:
            affected = sim.get_poles_for_target(con, body.scope, body.target_id, _graph)
            stale_ts = now - sim.STALE_THRESHOLD_S - 10
            for pole in affected:
                d_id = pole.get("device_id")
                p_id = pole.get("pole_id")
                if d_id:
                    con.execute(
                        "INSERT INTO device_last_seen (device_id, ts, pole_id) VALUES (?, ?, ?) "
                        "ON CONFLICT(device_id) DO UPDATE SET ts=?",
                        (d_id, stale_ts, p_id, stale_ts)
                    )
            con.commit()
            # Run detection pass immediately to process the suppression/ticket logic
            run_full_detection(con)
            
        con.commit()
        return {"id": outage_id, "status": "created"}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Detection trigger (for manual refresh from UI)
# ---------------------------------------------------------------------------

@app.post("/detect")
def trigger_detection():
    """Manually trigger a detection pass. Returns current pole statuses."""
    con = get_db()
    try:
        statuses = run_full_detection(con)
        counts = {}
        for s in statuses.values():
            counts[s] = counts.get(s, 0) + 1
        return {"status": "ok", "pole_status_counts": counts}
    finally:
        con.close()
