import sqlite3, time
con = sqlite3.connect('data/data.db')
con.row_factory = sqlite3.Row

tickets = con.execute("SELECT * FROM tickets").fetchall()
for t in tickets:
    d = dict(t)
    affected = d['affected_pole_ids'].split(',') if d['affected_pole_ids'] else []
    print(f"Ticket {d['id']}: status={d['status']}, affected poles={len(affected)}")

    # Check current status of each affected pole
    now = time.time()
    STALE_THRESHOLD_S = 900
    last_seen = {r[0]: r[1] for r in con.execute("SELECT device_id, ts FROM device_last_seen")}
    latest_event = {}
    for pid, ev, ts in con.execute("SELECT pole_id, event, ts FROM telemetry ORDER BY ts ASC"):
        latest_event[pid] = ev

    bad = []
    for pid in affected[:10]:  # check first 10
        device_id = con.execute("SELECT device_id FROM pole_registry WHERE pole_id=?", (pid,)).fetchone()[0]
        if not device_id:
            status = 'unknown'
        else:
            ev = latest_event.get(pid)
            seen_ts = last_seen.get(device_id)
            stale = seen_ts is None or (now - seen_ts) > STALE_THRESHOLD_S
            if ev == 'power_lost' or stale:
                status = 'fault'
            else:
                status = 'normal'
        if status != 'normal':
            bad.append((pid, status))

    print(f"  Non-normal poles in first 10: {bad}")
