"""
server.py

Backend for the operator console. Every pole's status is DERIVED from the
telemetry table via detection.compute_pole_statuses() on every request --
nothing is ever hand-set. See detection.py for the actual rules.
"""

import os
import sqlite3
import time
from pathlib import Path
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from graph_lib import build_graph
import telemetry_sim
import detection

DB_PATH = os.environ.get("KSPDB_DB", os.path.join(os.path.dirname(__file__), "data.db"))

BASE_DIR = Path(__file__).resolve().parent
FRONTEND_DIR = BASE_DIR / "frontend"

app = Flask(__name__)
CORS(app)

@app.route("/")
def index():
    if (FRONTEND_DIR / "index.html").exists():
        return send_from_directory(str(FRONTEND_DIR), "index.html")
    return jsonify({"status": "ok"})

@app.route("/css/<path:path>")
def serve_css(path):
    return send_from_directory(str(FRONTEND_DIR / "css"), path)

@app.route("/js/<path:path>")
def serve_js(path):
    return send_from_directory(str(FRONTEND_DIR / "js"), path)

@app.route("/fonts/<path:path>")
def serve_fonts(path):
    return send_from_directory(str(FRONTEND_DIR / "fonts"), path)

@app.route("/public/<path:path>")
def serve_public(path):
    return send_from_directory(str(FRONTEND_DIR / "public"), path)

_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph(DB_PATH)
    return _graph


def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def ensure_runtime_tables():
    con = get_db()
    cur = con.cursor()

    cur.execute("""CREATE TABLE IF NOT EXISTS telemetry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        device_id TEXT, pole_id TEXT, event TEXT, energized INTEGER,
        ts REAL, seq INTEGER, battery_mv INTEGER, rssi INTEGER, fw TEXT,
        received_at REAL,
        UNIQUE(device_id, seq, event)
    )""")
    # Indexes for fast pole-level and time-based lookups in detection + sim
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tel_pole ON telemetry(pole_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_tel_ts   ON telemetry(ts)")
    cur.execute("""CREATE TABLE IF NOT EXISTS device_last_seen (
        device_id TEXT PRIMARY KEY, ts REAL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS device_seq (
        device_id TEXT PRIMARY KEY, seq INTEGER
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY, scope TEXT, target_id TEXT,
        affected_pole_ids TEXT, status TEXT, created_at REAL
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS scheduled_outages (
        id TEXT PRIMARY KEY, scope TEXT NOT NULL, target_id TEXT NOT NULL,
        start_ts REAL NOT NULL, end_ts REAL NOT NULL, status TEXT NOT NULL DEFAULT 'active', reason TEXT
    )""")
    try:
        cur.execute("ALTER TABLE scheduled_outages ADD COLUMN status TEXT DEFAULT 'active'")
    except sqlite3.OperationalError:
        pass
    con.commit()

    # *** THE FIX ***
    # Seed a baseline "last seen just now" for every device that has never
    # been mentioned in device_last_seen. Without this, every device looks
    # like it's been silent since the dawn of time, and detection.py
    # (correctly, given what it's told) calls all of them stale -> fault.
    # INSERT OR IGNORE means this only fills in devices with NO row yet --
    # it will never stomp real staleness that has accumulated from actual
    # simulated faults, including across server restarts.
    now = time.time()
    device_ids = [r[0] for r in
                  cur.execute("SELECT device_id FROM pole_registry WHERE device_id IS NOT NULL")]
    cur.executemany("INSERT OR IGNORE INTO device_last_seen (device_id, ts) VALUES (?, ?)",
                     [(d, now) for d in device_ids])
    con.commit()
    con.close()



ensure_runtime_tables()


def ingest_batch(con, events):
    """Shared by /api/ingest and the fault simulator -- both paths produce
    the same telemetry shape and go through the same ingest logic, so the
    simulator is genuinely exercising the ingest+detection pipeline, not
    bypassing it."""
    cur = con.cursor()
    now = time.time()
    for e in events:
        cur.execute("""INSERT OR IGNORE INTO telemetry
            (device_id, pole_id, event, energized, ts, seq, battery_mv, rssi, fw, received_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (e["device_id"], e["pole_id"], e["event"], int(e.get("energized", False)),
             e["ts"], e["seq"], e.get("battery_mv"), e.get("rssi"), e.get("fw"), now))
        cur.execute("""INSERT INTO device_last_seen (device_id, ts) VALUES (?, ?)
                       ON CONFLICT(device_id) DO UPDATE SET ts=MAX(ts, excluded.ts)""",
                    (e["device_id"], e["ts"]))
    con.commit()


_static_topology_cache = None
_static_edges_cache = None

def get_static_topology():
    global _static_topology_cache
    if _static_topology_cache is None:
        con = get_db()
        cur = con.cursor()
        nodes = []
        for r in cur.execute("SELECT pole_id, lat, lon, dt_id, ward, pincode, device_id FROM pole_registry"):
            nodes.append({
                "id": r["pole_id"], "type": "pole", "lat": r["lat"], "lon": r["lon"],
                "dt_id": r["dt_id"], "ward": r["ward"], "pincode": r["pincode"],
                "has_device": r["device_id"] is not None,
            })
        for r in cur.execute("SELECT dt_id, lat, lon, households_served FROM transformer_registry"):
            nodes.append({"id": r["dt_id"], "type": "dt", "lat": r["lat"], "lon": r["lon"],
                           "households_served": r["households_served"]})
        for r in cur.execute("SELECT feeder_id, lat, lon FROM feeders"):
            nodes.append({"id": r["feeder_id"], "type": "feeder", "lat": r["lat"], "lon": r["lon"]})
        for r in cur.execute("SELECT substation_id, lat, lon FROM substations"):
            nodes.append({"id": r["substation_id"], "type": "substation", "lat": r["lat"], "lon": r["lon"]})
        con.close()
        _static_topology_cache = nodes
    return _static_topology_cache

def get_static_edges():
    global _static_edges_cache
    if _static_edges_cache is None:
        G = get_graph()
        edges = []
        for u, v, d in G.edges(data=True):
            un, vn = G.nodes[u], G.nodes[v]
            edges.append({"from": u, "to": v, "edge_type": d.get("edge_type"),
                           "from_lat": un["lat"], "from_lon": un["lon"],
                           "to_lat": vn["lat"], "to_lon": vn["lon"]})
        _static_edges_cache = edges
    return _static_edges_cache


@app.route("/ingest", methods=["POST"])
@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    """Real device-facing endpoint. Accepts one event or a list of events
    matching the payload shape in 02-data-and-systems.md section 2."""
    body = request.get_json(force=True)
    events = body if isinstance(body, list) else [body]
    con = get_db()
    ingest_batch(con, events)
    con.close()
    return jsonify({"ingested": len(events)})


@app.route("/network/topology")
@app.route("/api/network/topology")
def api_network_topology():
    return jsonify(get_static_topology())


@app.route("/network/status")
@app.route("/api/network/status")
def api_network_status():
    con = get_db()
    G = get_graph()
    statuses = detection.run_detection_pass(con, G)
    con.close()
    return jsonify(statuses)


@app.route("/network/edges")
@app.route("/api/network/edges")
def api_network_edges():
    return jsonify(get_static_edges())


@app.route("/stats")
@app.route("/api/stats")
def api_stats():
    con = get_db()
    G = get_graph()
    statuses = detection.run_detection_pass(con, G)
    cur = con.cursor()
    open_t = cur.execute("SELECT COUNT(*) FROM tickets WHERE status NOT IN ('verified','closed')").fetchone()[0]
    pole_c = cur.execute("SELECT COUNT(*) FROM pole_registry").fetchone()[0]
    fault_c = sum(1 for s in statuses.values() if s == "fault")
    active_f_c = detection.count_active_faults(statuses, G)
    con.close()
    return jsonify({
        "open_tickets": open_t,
        "pole_count": pole_c,
        "fault_pole_count": fault_c,
        "active_fault_count": active_f_c,
    })


@app.route("/health")
@app.route("/api/health")
def api_health():
    con = get_db()
    G = get_graph()
    statuses = detection.run_detection_pass(con, G)
    cur = con.cursor()
    open_t = cur.execute("SELECT COUNT(*) FROM tickets WHERE status NOT IN ('verified','closed')").fetchone()[0]
    pole_c = cur.execute("SELECT COUNT(*) FROM pole_registry").fetchone()[0]
    con.close()
    return jsonify({
        "status": "ok",
        "pole_count": pole_c,
        "open_tickets": open_t,
        "connected": True
    })


@app.route("/poles")
@app.route("/api/poles")
def api_poles():
    con = get_db()
    G = get_graph()
    statuses = detection.run_detection_pass(con, G)
    cur = con.cursor()
    rows = cur.execute("SELECT pole_id, lat, lon, dt_id, feeder_id, ward, pincode, device_id, seq_on_line, parent_pole_id FROM pole_registry").fetchall()
    con.close()
    res = []
    for r in rows:
        d = dict(r)
        d["status"] = statuses.get(r["pole_id"], "unknown")
        res.append(d)
    return jsonify(res)


@app.route("/transformers")
@app.route("/api/transformers")
def api_transformers():
    con = get_db()
    rows = con.execute("SELECT dt_id, feeder_id, lat, lon, capacity_kva, households_served FROM transformer_registry").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/feeders")
@app.route("/api/feeders")
def api_feeders():
    con = get_db()
    rows = con.execute("SELECT feeder_id, substation_id, lat, lon FROM feeders").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/substations")
@app.route("/api/substations")
def api_substations():
    con = get_db()
    rows = con.execute("SELECT substation_id, lat, lon FROM substations").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/network")
def api_network():
    con = get_db()
    cur = con.cursor()
    G = get_graph()

    statuses = detection.run_detection_pass(con, G)

    nodes = []
    for r in cur.execute("SELECT pole_id, lat, lon, dt_id, ward, pincode, device_id "
                          "FROM pole_registry"):
        nodes.append({
            "id": r["pole_id"], "type": "pole", "lat": r["lat"], "lon": r["lon"],
            "dt_id": r["dt_id"], "ward": r["ward"], "pincode": r["pincode"],
            "has_device": r["device_id"] is not None,
            "status": statuses.get(r["pole_id"], "unknown"),
        })
    for r in cur.execute("SELECT dt_id, lat, lon, households_served FROM transformer_registry"):
        nodes.append({"id": r["dt_id"], "type": "dt", "lat": r["lat"], "lon": r["lon"],
                       "households_served": r["households_served"]})
    for r in cur.execute("SELECT feeder_id, lat, lon FROM feeders"):
        nodes.append({"id": r["feeder_id"], "type": "feeder", "lat": r["lat"], "lon": r["lon"]})
    for r in cur.execute("SELECT substation_id, lat, lon FROM substations"):
        nodes.append({"id": r["substation_id"], "type": "substation", "lat": r["lat"], "lon": r["lon"]})

    edges = []
    for u, v, d in G.edges(data=True):
        un, vn = G.nodes[u], G.nodes[v]
        edges.append({"from": u, "to": v, "edge_type": d.get("edge_type"),
                       "from_lat": un["lat"], "from_lon": un["lon"],
                       "to_lat": vn["lat"], "to_lon": vn["lon"]})

    con.close()
    return jsonify({"nodes": nodes, "edges": edges})


@app.route("/poles/search")
@app.route("/api/poles/search")
def api_pole_search():
    q = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 20)), 50)
    con = get_db()
    cur = con.cursor()
    if q:
        like = f"%{q}%"
        rows = cur.execute(
            "SELECT pole_id, dt_id, ward FROM pole_registry "
            "WHERE pole_id LIKE ? OR dt_id LIKE ? OR ward LIKE ? LIMIT ?",
            (like, like, like, limit)).fetchall()
    else:
        rows = cur.execute("SELECT pole_id, dt_id, ward FROM pole_registry LIMIT ?", (limit,)).fetchall()
    con.close()
    return jsonify([{"pole_id": r["pole_id"], "dt_id": r["dt_id"], "ward": r["ward"]} for r in rows])


@app.route("/simulate/fault", methods=["POST"])
@app.route("/api/simulate/fault", methods=["POST"])
@app.route("/api/simulate-fault", methods=["POST"])
def api_simulate_fault():
    body = request.get_json(force=True)
    target_id = body.get("target_id")
    scope = body.get("scope") or body.get("type") or "pole"

    G = get_graph()
    if target_id not in G:
        return jsonify({"error": f"unknown id {target_id}"}), 400

    import networkx as nx
    affected = {target_id} | set(nx.descendants(G, target_id))
    affected_poles = [n for n in affected if G.nodes[n]["type"] == "pole"]

    con = get_db()
    cur = con.cursor()
    pole_device_pairs = []
    for pid in affected_poles:
        row = cur.execute("SELECT device_id FROM pole_registry WHERE pole_id=?", (pid,)).fetchone()
        pole_device_pairs.append((pid, row[0] if row else None))

    events, skipped = telemetry_sim.generate_fault_events(con, pole_device_pairs, fault_time=time.time())
    ingest_batch(con, events)
    statuses = detection.run_detection_pass(con, G)  # run immediately so the UI sees it without waiting for the next poll
    con.close()

    return jsonify({
        "affected_pole_count": len(affected_poles),
        "power_lost_messages_sent": len(events),
        "skipped": skipped[:50],  # why some poles sent nothing -- pedagogical, matches the spec's own failure rates
        "now_fault_status_count": sum(1 for p in affected_poles if statuses.get(p) == "fault"),
    })


@app.route("/tickets")
@app.route("/api/tickets")
def api_tickets():
    con = get_db()
    rows = con.execute("SELECT * FROM tickets ORDER BY created_at DESC LIMIT 100").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/tickets/<ticket_id>")
@app.route("/api/tickets/<ticket_id>")
def api_ticket_detail(ticket_id):
    con = get_db()
    row = con.execute("SELECT * FROM tickets WHERE id=?", (ticket_id,)).fetchone()
    con.close()
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.route("/tickets/<ticket_id>/acknowledge", methods=["POST"])
@app.route("/api/tickets/<ticket_id>/acknowledge", methods=["POST"])
def api_ticket_ack(ticket_id):
    con = get_db()
    now = time.time()
    con.execute("UPDATE tickets SET status='acknowledged', acknowledged_at=? WHERE id=?", (now, ticket_id))
    con.commit()
    con.close()
    return jsonify({"status": "acknowledged"})


@app.route("/tickets/<ticket_id>/assign-crew", methods=["POST"])
@app.route("/api/tickets/<ticket_id>/assign-crew", methods=["POST"])
def api_ticket_crew(ticket_id):
    con = get_db()
    now = time.time()
    con.execute("UPDATE tickets SET status='crew_assigned', crew_assigned_at=? WHERE id=?", (now, ticket_id))
    con.commit()
    con.close()
    return jsonify({"status": "crew_assigned"})


@app.route("/tickets/<ticket_id>/resolve", methods=["POST"])
@app.route("/api/tickets/<ticket_id>/resolve", methods=["POST"])
def api_ticket_resolve(ticket_id):
    con = get_db()
    now = time.time()
    con.execute("UPDATE tickets SET status='resolved', resolved_at=? WHERE id=?", (now, ticket_id))
    con.commit()
    con.close()
    return jsonify({"status": "resolved"})


@app.route("/simulate/restore", methods=["POST"])
@app.route("/api/simulate/restore", methods=["POST"])
@app.route("/api/simulate-restore", methods=["POST"])
def api_simulate_restore():
    body = request.get_json(force=True)
    target_id = body.get("target_id")
    scope = body.get("type") or body.get("scope") or "pole"
    
    con = get_db()
    G = get_graph()
    result = telemetry_sim.inject_restore_telemetry(con, scope, target_id, G)
    
    cur = con.cursor()
    now = time.time()
    try:
        cur.execute(
            "UPDATE tickets SET status='closed', verified_at=?, closed_at=? WHERE status NOT IN ('closed')",
            (now, now)
        )
        cur.execute(
            "UPDATE active_faults SET restored=1 WHERE target_id=? AND fault_type=? AND restored=0",
            (target_id, scope)
        )
        con.commit()
    except Exception:
        pass
    
    detection.run_detection_pass(con, G)
    con.commit()
    con.close()
    return jsonify(result)


@app.route("/api/simulate-load-shed", methods=["POST"])
def api_simulate_load_shed():
    """
    Schedule a load-shedding outage window.

    IMPORTANT: We do NOT backdate device_last_seen here.
    Load shedding is a *planned, controlled* outage — detection derives
    load_shed status by checking scheduled_outages, NOT telemetry staleness.
    Backdating last_seen causes the corroboration pass in detection to promote
    upstream siblings to 'fault', which is the bug that made upstream poles red.

    Real-world: only poles downstream of / under the target zone go dark.
    Upstream poles (closer to substation) remain energized and unaffected.
    """
    body = request.get_json(force=True)
    target_id = body.get("target_id")
    scope = body.get("scope")
    duration_minutes = int(body.get("duration_minutes", 60))
    start_delay_minutes = int(body.get("start_delay_minutes", 0))

    import uuid
    outage_id = f"SO-SIM-{uuid.uuid4().hex[:8]}"
    now = time.time()
    start_ts = now + start_delay_minutes * 60
    end_ts = start_ts + duration_minutes * 60

    con = get_db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO scheduled_outages (id, scope, target_id, start_ts, end_ts, status, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (outage_id, scope, target_id, start_ts, end_ts, "active", f"Load shedding ({duration_minutes} mins)")
    )
    con.commit()

    # Run detection so the map immediately reflects yellow load_shed status.
    # Detection reads scheduled_outages — no telemetry manipulation needed.
    G = get_graph()
    detection.run_detection_pass(con, G)
    con.commit()
    con.close()

    return jsonify({"id": outage_id, "status": "active", "end_ts": end_ts})


@app.route("/api/active-load-shed")
def api_active_load_shed():
    con = get_db()
    now = time.time()
    rows = con.execute(
        "SELECT * FROM scheduled_outages WHERE status = 'active' AND start_ts <= ? AND end_ts >= ?",
        (now, now)
    ).fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/end-load-shed/<outage_id>", methods=["POST"])
def api_end_load_shed(outage_id):
    con = get_db()
    cur = con.cursor()
    now = time.time()
    cur.execute(
        "UPDATE scheduled_outages SET status = 'ended', end_ts = ? WHERE id = ?",
        (now, outage_id)
    )
    
    outage = cur.execute("SELECT scope, target_id FROM scheduled_outages WHERE id = ?", (outage_id,)).fetchone()
    if outage:
        scope, target_id = outage["scope"], outage["target_id"]
        G = get_graph()
        affected = telemetry_sim.get_poles_for_target(con, scope, target_id, G)
        for pole in affected:
            d_id = pole.get("device_id")
            p_id = pole.get("pole_id")
            if d_id:
                cur.execute(
                    "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) VALUES (?, ?, ?)",
                    (d_id, now, p_id)
                )
        con.commit()
        detection.run_detection_pass(con, G)
    
    con.commit()
    con.close()
    return jsonify({"status": "ended"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=False)
