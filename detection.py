"""
detection.py

This is the actual fault-localization logic -- the part the assignment cares
about most, and the part it's easiest to accidentally shortcut. Nothing here
trusts a hand-set status field: everything is derived from the telemetry
table on every call.

v1 rules (deliberately simple -- documented limitations at the bottom):

  A pole is FAULT if:
    - its most recent telemetry event is `power_lost` with nothing more
      recent superseding it (a later `boot`/`power_restored` would win, since
      "most recent" already accounts for that), OR
    - its device has gone stale: no telemetry at all within STALE_THRESHOLD_S
      of now. This is what catches firmware-1.2 devices (never send
      power_lost) and the ~30% of dying messages that never arrive -- the
      two failure modes telemetry_sim.py deliberately produces.

  A pole is UNKNOWN if it has no device fitted at all (~9% of the fleet) --
  we have no signal, so we say so, rather than guessing normal.

  Otherwise a pole is NORMAL.

Grouping / tickets:
  Ticket scope is DT-level for v1: if any poles under a DT are FAULT, one
  ticket covers all of them, keyed on dt_id, so an operator gets one alert
  per transformer -- not one per pole (the "40 alerts for one wire" problem
  the brief calls out explicitly).

  This is coarser than it could be. The natural next step -- and the direct
  continuation of the topology-inference discussion -- is to cluster FAULT
  poles by span-adjacency where topology is known, so a fault on one branch
  of a DT doesn't lump in unaffected poles on a different branch of the same
  DT. Not built yet; noted here so it doesn't get lost.

  A ticket auto-verifies (from telemetry, not a button) when every pole
  originally on it goes back to NORMAL. There's no UI action yet for a crew
  to mark "resolved" first -- the doc's full detected -> acknowledged ->
  crew_assigned -> resolved -> verified -> closed lifecycle needs that button
  added; this collapses straight from detected to verified for now.
"""

import time

from telemetry_sim import STALE_THRESHOLD_S


def compute_pole_statuses(con, pole_rows):
    """pole_rows: iterable of (pole_id, device_id). Returns {pole_id: status}."""
    now = time.time()

    latest_event = {}
    for pole_id, event, ts in con.execute(
            "SELECT pole_id, event, ts FROM telemetry ORDER BY ts ASC"):
        latest_event[pole_id] = event  # ascending order -> last write wins -> most recent

    last_seen = {device_id: ts for device_id, ts in
                 con.execute("SELECT device_id, ts FROM device_last_seen")}

    statuses = {}
    for pole_id, device_id in pole_rows:
        if not device_id:
            statuses[pole_id] = "unknown"
            continue

        ev = latest_event.get(pole_id)
        seen_ts = last_seen.get(device_id)
        stale = seen_ts is None or (now - seen_ts) > STALE_THRESHOLD_S

        if ev == "power_lost" or stale:
            statuses[pole_id] = "fault"
        else:
            statuses[pole_id] = "normal"

    return statuses


def run_detection_pass(con, graph):
    """Computes statuses, opens tickets for newly-dark DTs, auto-verifies
    tickets whose poles have all recovered. Returns the status dict so the
    caller (an API handler) doesn't have to recompute it."""
    pole_rows = [(n, d.get("device_id")) for n, d in graph.nodes(data=True)
                 if d.get("type") == "pole"]
    statuses = compute_pole_statuses(con, pole_rows)

    # Which DT does each pole belong to?
    pole_dt = {n: d.get("dt_id") for n, d in graph.nodes(data=True) if d.get("type") == "pole"}

    dark_by_dt = {}
    for pole_id, status in statuses.items():
        if status == "fault":
            dark_by_dt.setdefault(pole_dt[pole_id], []).append(pole_id)

    now = time.time()
    cur = con.cursor()

    open_tickets = cur.execute(
        "SELECT id, target_id, affected_pole_ids FROM tickets "
        "WHERE status NOT IN ('verified','closed')").fetchall()
    open_by_dt = {t[1]: t for t in open_tickets}

    # Open new tickets for DTs that have fault poles and no open ticket yet
    for dt_id, dark_poles in dark_by_dt.items():
        if dt_id in open_by_dt:
            continue
        import uuid
        ticket_id = f"TCK-{uuid.uuid4().hex[:8]}"
        cur.execute("""INSERT INTO tickets (id, scope, target_id, affected_pole_ids, status, created_at)
                       VALUES (?, 'dt', ?, ?, 'detected', ?)""",
                    (ticket_id, dt_id, ",".join(dark_poles), now))

    # Auto-verify tickets whose poles are all back to normal
    for dt_id, ticket in open_by_dt.items():
        ticket_id, _, affected_csv = ticket
        affected = affected_csv.split(",") if affected_csv else []
        if affected and all(statuses.get(p) == "normal" for p in affected):
            cur.execute("UPDATE tickets SET status='verified' WHERE id=?", (ticket_id,))

    con.commit()
    return statuses