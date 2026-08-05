"""
Full pipeline test: inject fault → detect → verify restore → auto-close.
Runs in-process, no HTTP server needed.
"""
import sys, os, time, sqlite3
sys.path.insert(0, os.path.abspath('.'))

# Use an in-memory DB seeded with real pole data
import shutil
SRC_DB = 'data/data.db'
TEST_DB = 'data/test_pipeline.db'
shutil.copy2(SRC_DB, TEST_DB)

from build_graph import build_graph
from detection import run_detection_pass, compute_pole_statuses
from telemetry_sim import (inject_fault_telemetry, inject_restore_telemetry,
                            STALE_THRESHOLD_S)

# Init runtime tables
con = sqlite3.connect(TEST_DB)
con.row_factory = sqlite3.Row
con.execute("PRAGMA journal_mode=WAL")
con.executescript("""
    CREATE TABLE IF NOT EXISTS telemetry (
        id TEXT PRIMARY KEY, pole_id TEXT, device_id TEXT, event TEXT,
        energized INT, ts REAL, seq INT, battery_mv INT, rssi INT, fw TEXT,
        received_at REAL);
    CREATE TABLE IF NOT EXISTS device_last_seen (
        device_id TEXT PRIMARY KEY, ts REAL, pole_id TEXT);
    CREATE TABLE IF NOT EXISTS tickets (
        id TEXT PRIMARY KEY, scope TEXT, target_id TEXT, affected_pole_ids TEXT,
        status TEXT DEFAULT 'detected', confidence REAL, confidence_reason TEXT,
        lat REAL, lon REAL, pincode TEXT, feeder_id TEXT, created_at REAL,
        updated_at REAL, acknowledged_at REAL, crew_assigned_at REAL,
        resolved_at REAL, verified_at REAL, closed_at REAL);
    CREATE TABLE IF NOT EXISTS scheduled_outages (
        id TEXT PRIMARY KEY, scope TEXT, target_id TEXT,
        start_ts REAL, end_ts REAL, status TEXT DEFAULT 'active', reason TEXT);
    CREATE TABLE IF NOT EXISTS active_faults (
        id TEXT PRIMARY KEY, fault_type TEXT, target_id TEXT,
        injected_at REAL, restored INT DEFAULT 0);
""")
# Clear any residual runtime data from the copied DB
con.execute("DELETE FROM telemetry")
con.execute("DELETE FROM device_last_seen")
con.execute("DELETE FROM tickets")
con.execute("DELETE FROM scheduled_outages")
con.execute("DELETE FROM active_faults")
con.commit()

# Seed heartbeats — all poles recently seen
now = time.time()
rows = con.execute("SELECT device_id, pole_id FROM pole_registry WHERE device_id IS NOT NULL").fetchall()
con.executemany("INSERT OR IGNORE INTO device_last_seen (device_id, ts, pole_id) VALUES (?,?,?)",
                [(r[0], now - 60, r[1]) for r in rows])
con.commit()

g = build_graph(TEST_DB)
target_dt = 'D-0001'

# --- Step 1: All poles should be normal ---
statuses = compute_pole_statuses(con, [(n, d.get("device_id")) for n, d in g.nodes(data=True) if d.get("type") == "pole"])
fault_count = sum(1 for s in statuses.values() if s == 'fault')
print(f"[1] Before inject — fault poles: {fault_count} (expected: 0)")
assert fault_count == 0, f"Expected 0 fault poles, got {fault_count}"

# --- Step 2: Inject DT fault ---
result = inject_fault_telemetry(con, 'dt', target_dt, g)
print(f"[2] Injected: {result['affected_pole_count']} poles, {result['messages_generated']} msgs, {result['messages_lost']} lost")

# --- Step 3: Run detection — expect ticket ---
statuses = run_detection_pass(con, g)
fault_poles = sum(1 for s in statuses.values() if s == 'fault')
tickets = con.execute("SELECT * FROM tickets WHERE status NOT IN ('verified','closed')").fetchall()
print(f"[3] After inject — fault poles: {fault_poles}, open tickets: {len(tickets)}")
assert len(tickets) == 1, f"Expected 1 ticket, got {len(tickets)}"
t = dict(tickets[0])
print(f"    Ticket: {t['id']} target={t['target_id']} status={t['status']}")

# --- Step 4: Restore ---
restore_result = inject_restore_telemetry(con, 'dt', target_dt, g)
print(f"[4] Restore: {restore_result['messages_generated']} msgs sent")

# --- Step 5: Run detection — expect auto-verify ---
statuses = run_detection_pass(con, g)
tickets = con.execute("SELECT id, status, verified_at FROM tickets").fetchall()
print(f"[5] After restore — tickets:")
for tkt in tickets:
    d = dict(tkt)
    print(f"    {d['id']} status={d['status']} verified_at={d['verified_at']}")
    
verified = [t for t in tickets if dict(t)['status'] == 'verified']
assert len(verified) == 1, f"Expected 1 verified ticket, got {len(verified)}"
print(f"\n[OK] FULL PIPELINE TEST PASSED")

con.close()
os.remove(TEST_DB)
