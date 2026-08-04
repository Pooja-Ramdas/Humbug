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
from contextlib import asynccontextmanager
from typing import Optional, List

import networkx as nx
from fastapi import FastAPI, HTTPException, Query, Body
from fastapi.middleware.cors import CORSMiddleware
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
    con.commit()


def seed_heartbeats(con: sqlite3.Connection):
    """
    Seed initial device_last_seen entries for all devices so that poles show
    as 'normal' on first load rather than all appearing stale/fault.
    Only runs if device_last_seen is empty.
    """
    cur = con.cursor()
    count = cur.execute("SELECT COUNT(*) FROM device_last_seen").fetchone()[0]
    if count > 0:
        return  # already seeded

    now = time.time()
    rows = cur.execute(
        "SELECT device_id FROM pole_registry WHERE device_id IS NOT NULL"
    ).fetchall()
    cur.executemany(
        "INSERT OR IGNORE INTO device_last_seen (device_id, ts, pole_id) "
        "SELECT ?, ?, pole_id FROM pole_registry WHERE device_id = ?",
        [(r[0], now - 60, r[0]) for r in rows]  # 60s ago = recently seen
    )
    con.commit()
    log.info(f"Seeded {len(rows)} device heartbeats")

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

        # Check if this DT/feeder is under a scheduled outage right now
        feeder_row = cur.execute(
            "SELECT feeder_id FROM transformer_registry WHERE dt_id=?", (dt_id,)
        ).fetchone()
        feeder_id = feeder_row["feeder_id"] if feeder_row else None

        is_scheduled = cur.execute(
            "SELECT id FROM scheduled_outages WHERE start_ts <= ? AND end_ts >= ? "
            "AND (target_id=? OR target_id=?)",
            (now, now, dt_id, feeder_id or "")
        ).fetchone()

        if is_scheduled:
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
    # Auto-close tickets that have been verified
    cur.execute(
        "UPDATE tickets SET status='closed', closed_at=?, updated_at=? "
        "WHERE status='verified' AND verified_at IS NOT NULL AND closed_at IS NULL",
        (now2, now2)
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
    con = get_db()
    init_runtime_tables(con)
    seed_heartbeats(con)
    con.close()
    _graph = build_graph(DB_PATH)
    log.info(f"Graph loaded: {_graph.number_of_nodes()} nodes, {_graph.number_of_edges()} edges")
    yield
    log.info("Humbug backend shutting down")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Humbug", version="1.0.0", lifespan=lifespan)

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
        pole_count = con.execute("SELECT COUNT(*) FROM pole_registry").fetchone()[0]
        open_tickets = con.execute(
            "SELECT COUNT(*) FROM tickets WHERE status NOT IN ('verified','closed','detected') "
            "OR status = 'detected'"
        ).fetchone()[0]
        fault_poles = con.execute(
            "SELECT COUNT(*) FROM tickets t WHERE t.status NOT IN ('verified','closed')"
        ).fetchone()[0]
        dt_count = con.execute("SELECT COUNT(*) FROM transformer_registry").fetchone()[0]
        feeder_count = con.execute("SELECT COUNT(*) FROM feeders").fetchone()[0]
        return {
            "pole_count": pole_count, "dt_count": dt_count,
            "feeder_count": feeder_count, "open_tickets": open_tickets,
            "fault_poles": fault_poles,
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
        now = time.time()
        outage_targets = set()
        outages = cur.execute(
            "SELECT scope, target_id FROM scheduled_outages "
            "WHERE start_ts <= ? AND end_ts >= ?", (now, now)
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

            # Override: if pole is under a scheduled outage, show load_shedding
            final_status = raw_status
            if raw_status == "fault":
                dt_id = p["dt_id"]
                feeder_id = p["feeder_id"]
                if ("feeder", feeder_id) in outage_targets or ("dt", dt_id) in outage_targets:
                    final_status = "load_shedding"

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
