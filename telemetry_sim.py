"""
telemetry_sim.py

Fault simulator and telemetry generator for the Humbug fault-localization system.

This module is imported by both the backend API (for the /simulate/* endpoints)
and can be run standalone. It understands the physics of the network:
  - A span fault darkens every pole downstream of the broken span.
  - A DT fault darkens every pole under that transformer.
  - A feeder fault darkens every pole under every DT on that feeder.
  - ~30% of dying messages never arrive (fw >= 1.3).
  - fw 1.2.x devices (~8% of fleet) never send power_lost at all — they
    just stop heartbeating. The stale-threshold logic in detection.py catches
    these after STALE_THRESHOLD_S seconds of silence.
  - Restoration sends boot + power_restored for all affected poles.

STALE_THRESHOLD_S is the canonical constant the rest of the codebase imports
from here. It must match the heartbeat interval (900s = 15 min).
"""

import random
import time
import uuid
import sqlite3
from typing import List, Dict, Any, Optional

# Heartbeat every 15 minutes ± 45s jitter.
# A pole is considered stale if we haven't heard from it in one full interval.
STALE_THRESHOLD_S = 900   # 15 minutes

# Fraction of fw >= 1.3 devices whose dying message is lost in transit
DYING_MSG_LOSS_RATE = 0.30

# Fraction of fleet on fw 1.2.x (never sends power_lost at all)
FW12_FRACTION = 0.08

# Firmware version strings
FW_12_VERSION = "1.2.3"
FW_13_VERSION = "1.4.2"


def _random_fw(device_id: str) -> str:
    """Deterministic firmware assignment based on device_id hash so it's stable."""
    h = hash(device_id) % 100
    return FW_12_VERSION if h < int(FW12_FRACTION * 100) else FW_13_VERSION


def _ts_now_with_skew() -> float:
    """Return current Unix timestamp with up to ±90s device clock skew."""
    return time.time() + random.uniform(-90, 90)


def _make_event(pole_id: str, device_id: str, event: str,
                energized: bool, seq: int, fw: str) -> Dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "pole_id": pole_id,
        "device_id": device_id,
        "event": event,
        "energized": energized,
        "ts": _ts_now_with_skew(),
        "seq": seq,
        "battery_mv": random.randint(3100, 3700),
        "rssi": random.randint(-105, -65),
        "fw": fw,
        "received_at": time.time(),
    }


def get_poles_for_target(con: sqlite3.Connection, fault_type: str,
                          target_id: str, graph=None) -> List[Dict]:
    """
    Return all poles that would be affected by the given fault.

    - span fault: poles downstream of the span (pole and all descendants).
      Requires graph for topology walk; falls back to DT-level if not available.
    - dt fault: all poles under target DT.
    - feeder fault: all poles under all DTs on the feeder.

    Returns list of dicts with keys: pole_id, device_id, lat, lon, dt_id, feeder_id.
    """
    cur = con.cursor()
    affected = []

    if fault_type == "feeder":
        rows = cur.execute(
            "SELECT pole_id, device_id, lat, lon, dt_id, feeder_id "
            "FROM pole_registry WHERE feeder_id = ?", (target_id,)
        ).fetchall()
        affected = [dict(zip(["pole_id","device_id","lat","lon","dt_id","feeder_id"], r))
                    for r in rows]

    elif fault_type == "dt":
        rows = cur.execute(
            "SELECT pole_id, device_id, lat, lon, dt_id, feeder_id "
            "FROM pole_registry WHERE dt_id = ?", (target_id,)
        ).fetchall()
        affected = [dict(zip(["pole_id","device_id","lat","lon","dt_id","feeder_id"], r))
                    for r in rows]

    elif fault_type == "span":
        # target_id is a pole_id; we darken that pole and everything downstream of it.
        # Use graph topology if available, otherwise fall back to DT-level.
        if graph is not None:
            import networkx as nx
            if target_id in graph:
                downstream = nx.descendants(graph, target_id)
                downstream.add(target_id)
                pole_ids = [n for n in downstream
                            if graph.nodes[n].get("type") == "pole"]
                if pole_ids:
                    placeholders = ",".join("?" * len(pole_ids))
                    rows = cur.execute(
                        f"SELECT pole_id, device_id, lat, lon, dt_id, feeder_id "
                        f"FROM pole_registry WHERE pole_id IN ({placeholders})",
                        pole_ids
                    ).fetchall()
                    affected = [dict(zip(["pole_id","device_id","lat","lon","dt_id","feeder_id"], r))
                                for r in rows]

        if not affected:
            # Fallback: just the target pole itself
            row = cur.execute(
                "SELECT pole_id, device_id, lat, lon, dt_id, feeder_id "
                "FROM pole_registry WHERE pole_id = ?", (target_id,)
            ).fetchone()
            if row:
                affected = [dict(zip(["pole_id","device_id","lat","lon","dt_id","feeder_id"], row))]

    return affected


def inject_fault_telemetry(con: sqlite3.Connection, fault_type: str,
                            target_id: str, graph=None) -> Dict[str, Any]:
    """
    Simulate a fault. Generates power_lost telemetry for affected poles,
    honouring fw-1.2 silence and the 30% message-loss rate.

    Returns a summary dict with affected_poles, messages_generated, messages_lost.
    """
    affected = get_poles_for_target(con, fault_type, target_id, graph)
    if not affected:
        return {"error": f"No poles found for {fault_type} target {target_id}"}

    cur = con.cursor()

    # Get current seq numbers per pole
    seq_map: Dict[str, int] = {}
    for pole in affected:
        row = cur.execute(
            "SELECT MAX(seq) FROM telemetry WHERE pole_id = ?",
            (pole["pole_id"],)
        ).fetchone()
        seq_map[pole["pole_id"]] = (row[0] or 0) + 1

    messages_generated = 0
    messages_lost = 0
    inserted = []

    for pole in affected:
        device_id = pole.get("device_id")
        if not device_id:
            # No device — stays in "unknown" status, nothing to emit
            continue

        fw = _random_fw(device_id)
        pole_id = pole["pole_id"]
        seq = seq_map[pole_id]

        if fw == FW_12_VERSION:
            # fw 1.2 — just goes silent, no power_lost message ever sent
            # We update device_last_seen to a stale timestamp so detection
            # catches it after STALE_THRESHOLD_S.
            stale_ts = time.time() - STALE_THRESHOLD_S - random.uniform(1, 60)
            cur.execute(
                "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) "
                "VALUES (?, ?, ?)", (device_id, stale_ts, pole_id)
            )
            messages_lost += 1  # conceptually "lost" (never sent)
            continue

        # fw >= 1.3 — attempts to send power_lost
        if random.random() < DYING_MSG_LOSS_RATE:
            # Message lost in transit — simulate by making last_seen stale too
            stale_ts = time.time() - STALE_THRESHOLD_S - random.uniform(1, 60)
            cur.execute(
                "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) "
                "VALUES (?, ?, ?)", (device_id, stale_ts, pole_id)
            )
            messages_lost += 1
            continue

        # Message delivered
        event = _make_event(pole_id, device_id, "power_lost", False, seq, fw)
        cur.execute(
            "INSERT INTO telemetry (id, pole_id, device_id, event, energized, "
            "ts, seq, battery_mv, rssi, fw, received_at) VALUES "
            "(:id,:pole_id,:device_id,:event,:energized,:ts,:seq,:battery_mv,:rssi,:fw,:received_at)",
            event
        )
        cur.execute(
            "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) "
            "VALUES (?, ?, ?)", (device_id, event["ts"], pole_id)
        )
        inserted.append(event)
        messages_generated += 1

    con.commit()

    return {
        "fault_type": fault_type,
        "target_id": target_id,
        "affected_pole_count": len(affected),
        "device_equipped_count": sum(1 for p in affected if p.get("device_id")),
        "messages_generated": messages_generated,
        "messages_lost": messages_lost,
        "fw12_silent": sum(
            1 for p in affected
            if p.get("device_id") and _random_fw(p["device_id"]) == FW_12_VERSION
        ),
        "affected_poles": [p["pole_id"] for p in affected],
    }


def inject_restore_telemetry(con: sqlite3.Connection, fault_type: str,
                              target_id: str, graph=None) -> Dict[str, Any]:
    """
    Simulate fault repair. Sends boot + power_restored for all affected poles.
    Also refreshes device_last_seen to now (so stale detection clears).
    """
    affected = get_poles_for_target(con, fault_type, target_id, graph)
    if not affected:
        return {"error": f"No poles found for {fault_type} target {target_id}"}

    cur = con.cursor()
    messages_generated = 0

    for pole in affected:
        device_id = pole.get("device_id")
        if not device_id:
            continue

        fw = _random_fw(device_id)
        pole_id = pole["pole_id"]

        row = cur.execute(
            "SELECT MAX(seq) FROM telemetry WHERE pole_id = ?", (pole_id,)
        ).fetchone()
        seq = (row[0] or 0) + 1

        # boot event
        boot_event = _make_event(pole_id, device_id, "boot", True, seq, fw)
        cur.execute(
            "INSERT INTO telemetry (id, pole_id, device_id, event, energized, "
            "ts, seq, battery_mv, rssi, fw, received_at) VALUES "
            "(:id,:pole_id,:device_id,:event,:energized,:ts,:seq,:battery_mv,:rssi,:fw,:received_at)",
            boot_event
        )

        # power_restored event
        restored_event = _make_event(pole_id, device_id, "power_restored", True, seq + 1, fw)
        cur.execute(
            "INSERT INTO telemetry (id, pole_id, device_id, event, energized, "
            "ts, seq, battery_mv, rssi, fw, received_at) VALUES "
            "(:id,:pole_id,:device_id,:event,:energized,:ts,:seq,:battery_mv,:rssi,:fw,:received_at)",
            restored_event
        )

        # Refresh last_seen to now so stale detection clears
        cur.execute(
            "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) "
            "VALUES (?, ?, ?)", (device_id, time.time(), pole_id)
        )
        messages_generated += 2

    con.commit()

    return {
        "fault_type": fault_type,
        "target_id": target_id,
        "affected_pole_count": len(affected),
        "messages_generated": messages_generated,
    }


def inject_noise_device_death(con: sqlite3.Connection,
                               pole_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Simulate a device dying while power is fine.
    Picks a random pole (or the specified one) and makes its device_last_seen stale.
    This should NOT produce a fault ticket because the pole's children stay live
    (detection.py checks for this implicitly — a single isolated dark pole with
    live children is physically impossible as a line fault).
    NOTE: v1 detection doesn't yet implement the "live children" check, so this
    will currently show as fault. That's documented as a known limitation.
    """
    cur = con.cursor()
    if not pole_id:
        row = cur.execute(
            "SELECT pole_id, device_id FROM pole_registry "
            "WHERE device_id IS NOT NULL ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    else:
        row = cur.execute(
            "SELECT pole_id, device_id FROM pole_registry "
            "WHERE pole_id = ?", (pole_id,)
        ).fetchone()

    if not row:
        return {"error": "No suitable pole found"}

    p_id, d_id = row
    stale_ts = time.time() - STALE_THRESHOLD_S - 120
    cur.execute(
        "INSERT OR REPLACE INTO device_last_seen (device_id, ts, pole_id) "
        "VALUES (?, ?, ?)", (d_id, stale_ts, p_id)
    )
    con.commit()
    return {"noise_type": "device_death", "pole_id": p_id, "device_id": d_id}


def inject_noise_scheduled_outage(con: sqlite3.Connection, scope: str,
                                   target_id: str, duration_minutes: int = 60) -> Dict[str, Any]:
    """
    Register a scheduled outage window. Poles going dark during this window
    should not become fault tickets.
    """
    cur = con.cursor()
    start_ts = time.time()
    end_ts = start_ts + duration_minutes * 60
    outage_id = f"SO-SIM-{uuid.uuid4().hex[:8]}"
    cur.execute(
        "INSERT INTO scheduled_outages (id, scope, target_id, start_ts, end_ts, reason) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (outage_id, scope, target_id, start_ts, end_ts, "Simulated load shedding")
    )
    con.commit()
    return {"noise_type": "scheduled_outage", "id": outage_id, "scope": scope,
            "target_id": target_id, "duration_minutes": duration_minutes}


def inject_noise_duplicate_burst(con: sqlite3.Connection,
                                  pole_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Simulate at-least-once delivery: inject duplicate telemetry messages.
    The ingest dedup logic (pole_id + seq) should absorb these without
    creating duplicate state changes.
    """
    cur = con.cursor()
    if not pole_id:
        row = cur.execute(
            "SELECT pole_id, device_id FROM pole_registry "
            "WHERE device_id IS NOT NULL ORDER BY RANDOM() LIMIT 1"
        ).fetchone()
    else:
        row = cur.execute(
            "SELECT pole_id, device_id FROM pole_registry WHERE pole_id = ?",
            (pole_id,)
        ).fetchone()

    if not row:
        return {"error": "No suitable pole found"}

    p_id, d_id = row
    fw = _random_fw(d_id)

    # Get last known seq for this pole
    last = cur.execute(
        "SELECT MAX(seq) FROM telemetry WHERE pole_id = ?", (p_id,)
    ).fetchone()
    seq = last[0] or 1

    # Insert 3 duplicate heartbeats with the same seq (simulating retry storm)
    inserted_count = 0
    for _ in range(3):
        event = _make_event(p_id, d_id, "heartbeat", True, seq, fw)
        try:
            cur.execute(
                "INSERT INTO telemetry (id, pole_id, device_id, event, energized, "
                "ts, seq, battery_mv, rssi, fw, received_at) VALUES "
                "(:id,:pole_id,:device_id,:event,:energized,:ts,:seq,:battery_mv,:rssi,:fw,:received_at)",
                event
            )
            inserted_count += 1
        except Exception:
            pass

    con.commit()
    return {"noise_type": "duplicate_burst", "pole_id": p_id, "duplicates_sent": inserted_count,
            "note": "ingest dedup by pole_id+seq should absorb these"}
