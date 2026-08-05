import sqlite3, time
con = sqlite3.connect('data/data.db')
STALE_THRESHOLD_S = 900
now = time.time()

# Check what events we have for first few poles of D-0001
poles = con.execute(
    "SELECT pole_id, device_id FROM pole_registry WHERE dt_id='D-0001' LIMIT 5"
).fetchall()

last_seen = {r[0]: r[1] for r in con.execute("SELECT device_id, ts FROM device_last_seen")}

for pole_id, device_id in poles:
    events = con.execute(
        "SELECT event, ts FROM telemetry WHERE pole_id=? ORDER BY ts DESC LIMIT 3",
        (pole_id,)
    ).fetchall()
    ev_latest = events[0][0] if events else None
    seen_ts = last_seen.get(device_id)
    stale = seen_ts is None or (now - seen_ts) > STALE_THRESHOLD_S
    print(f"{pole_id} device={device_id}")
    print(f"  latest_event={ev_latest}, last_seen_age={round(now-seen_ts,1) if seen_ts else 'never'}s, stale={stale}")
    for ev, ts in events:
        print(f"  {ev} at {round(now-ts,1)}s ago")
