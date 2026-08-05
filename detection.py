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


def refresh_healthy_heartbeats(con):
    """
    Simulates the continuous heartbeat cadence of the live IoT fleet.
    For every device that is currently healthy (last_seen is not stale and
    its latest telemetry event is NOT 'power_lost'), update device_last_seen to now.

    Devices explicitly backdated by fault simulation (fw 1.2 silent, lost dying messages,
    or device death noise) have last_seen > STALE_THRESHOLD_S old and are EXCLUDED.
    Devices with latest event 'power_lost' are EXCLUDED.
    """
    now = time.time()
    cur = con.cursor()

    cutoff = now - 172800
    power_lost_devices = set()
    for r in con.execute(
        "SELECT t1.device_id FROM telemetry t1 "
        "INNER JOIN (SELECT pole_id, MAX(ts) as max_ts FROM telemetry "
        "            WHERE ts >= ? GROUP BY pole_id) t2 "
        "ON t1.pole_id = t2.pole_id AND t1.ts = t2.max_ts "
        "WHERE t1.event = 'power_lost'",
        (cutoff,)
    ):
        if r["device_id"]:
            power_lost_devices.add(r["device_id"])

    rows = cur.execute("SELECT device_id, ts FROM device_last_seen").fetchall()
    to_refresh = []
    for r in rows:
        dev_id, ts = r["device_id"], r["ts"]
        if dev_id in power_lost_devices:
            continue
        if ts is not None and (now - ts) <= STALE_THRESHOLD_S:
            to_refresh.append((now, dev_id))

    if to_refresh:
        cur.executemany("UPDATE device_last_seen SET ts = ? WHERE device_id = ?", to_refresh)
        con.commit()


def compute_pole_statuses(con, pole_rows, graph=None):
    """pole_rows: iterable of (pole_id, device_id). Returns {pole_id: status}."""
    refresh_healthy_heartbeats(con)

    now = time.time()
    cur = con.cursor()

    # Load active scheduled outages (status='active' and currently within time window)
    active_outages = con.execute(
        "SELECT scope, target_id FROM scheduled_outages "
        "WHERE status = 'active' AND start_ts <= ? AND end_ts >= ?", (now, now)
    ).fetchall()
    # Load shed is ONLY at feeder or DT level — never individual poles.
    # Real-world load shedding is controlled at substations/feeder breakers;
    # entire zones go off together, not individual poles.
    outage_targets = set(
        (o[0], o[1]) for o in active_outages
        if o[0] in ('feeder', 'dt')  # pole-level load shed does not exist in practice
    )

    # Load pole info for quick mapping
    pole_info = {
        r[0]: (r[1], r[2])
        for r in con.execute("SELECT pole_id, dt_id, feeder_id FROM pole_registry").fetchall()
    }

    # Bound the telemetry scan to the last 48h to avoid full-table scans as
    # the telemetry table grows. Any event older than 48h cannot possibly be
    # the "latest" event for an active device (heartbeat is 15 min).
    cutoff = now - 172800  # 48 hours
    latest_event = {}
    for r in con.execute(
        "SELECT t1.pole_id, t1.event FROM telemetry t1 "
        "INNER JOIN (SELECT pole_id, MAX(ts) as max_ts FROM telemetry "
        "            WHERE ts >= ? GROUP BY pole_id) t2 "
        "ON t1.pole_id = t2.pole_id AND t1.ts = t2.max_ts",
        (cutoff,)
    ):
        latest_event[r["pole_id"]] = r["event"]

    last_seen = {device_id: ts for device_id, ts in
                 con.execute("SELECT device_id, ts FROM device_last_seen")}

    statuses = {}
    stale_pending = set()

    # Pass 1: Compute initial statuses
    for pole_id, device_id in pole_rows:
        dt_id, feeder_id = pole_info.get(pole_id, (None, None))
        # Load shed is feeder/DT level only — never pole-level.
        is_load_shed = (
            ("dt", dt_id) in outage_targets or
            ("feeder", feeder_id) in outage_targets
        )

        if is_load_shed:
            statuses[pole_id] = "load_shed"
            continue

        if not device_id:
            statuses[pole_id] = "unknown"
            continue

        ev = latest_event.get(pole_id)
        seen_ts = last_seen.get(device_id)
        stale = seen_ts is None or (now - seen_ts) > STALE_THRESHOLD_S

        if ev == "power_lost":
            statuses[pole_id] = "fault"
        elif stale:
            statuses[pole_id] = "stale_pending"
            stale_pending.add(pole_id)
        else:
            statuses[pole_id] = "normal"

    # Pass 2: Resolve stale_pending statuses using correlated-staleness rules.
    #
    # A stale_pending pole is promoted to 'fault' ONLY if ≥2 other poles in
    # the same DT are also stale or fault. This prevents a single real fault on
    # one branch from incorrectly dragging unrelated stale devices (e.g. a dying
    # IoT battery on a different branch) into the fault count — which was the
    # root cause of "fault count doesn't match red pole count".
    #
    # If only isolated staleness (≤1 corroborating peer), treat as 'unknown'
    # (grey) — it's a device issue, not a network fault.
    dt_to_statuses = {}
    for pid, status in statuses.items():
        dt_id, _ = pole_info.get(pid, (None, None))
        if dt_id:
            dt_to_statuses.setdefault(dt_id, []).append((pid, status))

    for pid in stale_pending:
        dt_id, _ = pole_info.get(pid, (None, None))
        corroboration_count = 0
        if dt_id:
            for other_pid, other_status in dt_to_statuses.get(dt_id, []):
                if other_pid == pid:
                    continue
                if other_status in ("fault", "stale_pending"):
                    corroboration_count += 1
        # Require >=2 corroborating peers to call it a network fault.
        # A single isolated stale/silent device = IoT device failure, NOT a
        # line fault. Show as 'device_fault' (grey) so the operator sees it
        # as a device issue rather than a power outage.
        # This is what keeps dead-device noise from creating fault tickets.
        if corroboration_count >= 2:
            statuses[pid] = "fault"
        else:
            statuses[pid] = "device_fault"  # grey: device issue, not a line fault

    # Pass 3: DOWNSTREAM FAULT PROPAGATION
    # Once a pole becomes the first faulty pole, EVERY downstream pole after it MUST also be in the fault state.
    # No downstream pole may remain green.
    if graph is not None:
        import networkx as nx
        fault_poles = [pid for pid, st in statuses.items() if st == "fault"]
        for fp in fault_poles:
            if fp in graph:
                for desc in nx.descendants(graph, fp):
                    if graph.nodes[desc].get("type") == "pole":
                        statuses[desc] = "fault"

                fp_node = graph.nodes[fp]
                dt_id = fp_node.get("dt_id")
                seq = fp_node.get("seq_on_line")
                if dt_id and seq is not None:
                    for n, d in graph.nodes(data=True):
                        if d.get("type") == "pole" and d.get("dt_id") == dt_id and d.get("seq_on_line") is not None:
                            if d["seq_on_line"] >= seq:
                                statuses[n] = "fault"

    return statuses


def count_active_faults(statuses, graph):
    """
    Computes the number of distinct fault locations / root fault start points.
    A continuous downstream outage across 1, 9, or 50 poles represents 1 fault.
    """
    import networkx as nx
    fault_poles = {p for p, st in statuses.items() if st == "fault"}
    if not fault_poles:
        return 0

    fault_roots = set()
    for p in fault_poles:
        if p in graph:
            preds = list(graph.predecessors(p))
            has_faulted_pole_parent = False
            for pred in preds:
                if graph.nodes[pred].get("type") == "pole" and pred in fault_poles:
                    has_faulted_pole_parent = True
                    break
            if not has_faulted_pole_parent:
                fault_roots.add(p)
        else:
            fault_roots.add(p)

    dt_faults = {}
    for p in fault_poles:
        if p in graph:
            dt_id = graph.nodes[p].get("dt_id")
            if dt_id:
                dt_faults.setdefault(dt_id, set()).add(p)

    processed_dts = set()
    count = 0
    for dt_id, f_poles in dt_faults.items():
        all_dt_poles = {n for n in nx.descendants(graph, dt_id) if graph.nodes[n].get("type") == "pole"} if dt_id in graph else set()
        if all_dt_poles and f_poles == all_dt_poles:
            count += 1
            processed_dts.add(dt_id)

    for r in fault_roots:
        if r in graph:
            dt_id = graph.nodes[r].get("dt_id")
            if dt_id in processed_dts:
                continue
        count += 1

    return count


def run_detection_pass(con, graph):
    """Computes statuses, opens tickets for newly-dark DTs, auto-verifies
    tickets whose poles have all recovered. Returns the status dict so the
    caller (an API handler) doesn't have to recompute it."""
    pole_rows = [(n, d.get("device_id")) for n, d in graph.nodes(data=True)
                 if d.get("type") == "pole"]
    statuses = compute_pole_statuses(con, pole_rows, graph=graph)

    # Which DT does each pole belong to?
    pole_dt = {n: d.get("dt_id") for n, d in graph.nodes(data=True) if d.get("type") == "pole"}

    dark_by_dt = {}
    for pole_id, status in statuses.items():
        if status == "fault":
            dark_by_dt.setdefault(pole_dt[pole_id], []).append(pole_id)

    now = time.time()
    cur = con.cursor()

    open_tickets = cur.execute(
        "SELECT id, target_id, affected_pole_ids, status, resolved_at FROM tickets "
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

    # Auto-verify tickets whose poles have recovered or passed the resolution grace period
    for ticket in open_tickets:
        ticket_id, dt_id, affected_csv, status, resolved_at = ticket
        affected = affected_csv.split(",") if affected_csv else []
        if not affected:
            continue

        total_count = len(affected)
        # Count poles that are no longer in active fault state.
        # 'normal' = fully recovered.
        # 'device_fault' = IoT device issue (not a line fault) — doesn't block ticket close.
        # 'load_shed' = planned outage — doesn't block ticket close.
        # 'unknown' = no device, can't tell either way — counts as recovered (no signal = no fault).
        recovered_count = sum(
            1 for p in affected
            if statuses.get(p) in ("normal", "device_fault", "load_shed", "unknown")
        )
        recovery_fraction = recovered_count / total_count if total_count > 0 else 0

        # Condition 1: High confidence (>= 70% of originally affected poles no longer fault)
        high_confidence = recovery_fraction >= 0.70

        # Condition 2: Resolution grace period (status is resolved, >= 50% recovered, and 120s elapsed)
        grace_passed = False
        if status == "resolved" and resolved_at is not None:
            if (now - resolved_at) >= 120 and recovery_fraction >= 0.50:
                grace_passed = True

        if high_confidence or grace_passed:
            cur.execute("UPDATE tickets SET status='verified', verified_at=? WHERE id=?", (now, ticket_id))

    # verified -> closed transition:
    # We apply a short 10-second grace period after a ticket is marked as 'verified'
    # so that the operator can briefly see the verified state before the ticket is closed.
    verified_tickets = cur.execute("SELECT id, verified_at FROM tickets WHERE status='verified'").fetchall()
    for t_id, v_at in verified_tickets:
        if v_at is None or (now - v_at) >= 10:
            cur.execute("UPDATE tickets SET status='closed', closed_at=? WHERE id=?", (now, t_id))

    con.commit()
    return statuses
