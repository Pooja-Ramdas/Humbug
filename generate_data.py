"""
generate_data.py

Generates a synthetic pole registry, transformer registry, and feeder/substation
hierarchy for the KSPDB fault-localization assignment, matching the schema and
"dirtiness" statistics described in 02-data-and-systems.md.

Design notes (read this before you touch the numbers):

- We do NOT generate all 38,400 real-world poles. The brief explicitly says a
  "few thousand poles" with a realistic *shape* is enough. We scale the
  substation/feeder/DT counts down but keep the per-DT distributions (poles
  per DT, branch count, line length) matching the spec, so the statistical
  texture is right even though total volume is smaller.

- Pole placement is NOT random scatter. Real LT lines follow roads/right-of-way,
  so each branch is generated as a correlated random walk (small heading
  changes between consecutive poles) rather than independent random points.
  This matters later: your geometric MST inference (Tier 2) will look
  artificially good if poles are randomly scattered, because "nearest
  neighbor" trivially reconstructs a random point cloud's MST. A road-like
  walk is a fairer test.

- We generate the FULL ground truth topology for every pole first, then
  corrupt a copy of it to match the spec:
    * seq_on_line / parent_pole_id stripped for ~60% of DTs (chosen whole,
      not per-pole -- the doc says whole DTs are missing this, not random
      poles within a DT)
    * device_id missing for ~9% of poles (independent, random)
    * pincode missing for ~3% of poles (independent, random)

  The uncorrupted ground truth is written to a SEPARATE table/file
  (ground_truth_topology) that your actual system must never read at
  runtime. It exists purely so you can later validate a geometric/learned
  inference algorithm by masking known DTs and checking how often you'd
  have guessed right.

Usage:
    python3 generate_data.py [--seed 42] [--n-dts 40] [--out data.db]
"""

import argparse
import csv
import math
import random
import sqlite3
import os

# ---------------------------------------------------------------------------
# Config / constants matching the assignment's stated distributions
# ---------------------------------------------------------------------------

N_SUBSTATIONS = 4
FEEDERS_PER_SUBSTATION = 2          # -> 8 feeders total (scaled down from 31)
DTS_PER_FEEDER = 5                  # -> 40 DTs total (scaled down from 412)

POLES_PER_DT_MEDIAN = 70
POLES_PER_DT_MIN = 9
POLES_PER_DT_MAX = 240

MAX_BRANCH_LENGTH_M = 1400          # "up to ~1.4 km from the transformer"
MIN_POLE_SPACING_M = 15
MAX_POLE_SPACING_M = 40
BRANCH_COUNT_RANGE = (1, 5)         # "one to five branches off the main run"

MISSING_TOPOLOGY_DT_FRACTION = 0.60
MISSING_DEVICE_ID_FRACTION = 0.09
MISSING_PINCODE_FRACTION = 0.03

# Rough Bangalore-area bounding box for the "one subdivision" -- doesn't need
# to be real, just plausible and internally consistent.
CITY_CENTER_LAT = 12.9716
CITY_CENTER_LON = 77.5946
SUBDIVISION_SPREAD_M = 6000  # ~6km box for the whole subdivision

PINCODES = ["560078", "560068", "560034", "560095"]

POLE_TYPES = ["LT-9m-PCC", "LT-8m-Steel", "LT-9m-Steel", "LT-8m-PCC"]


# ---------------------------------------------------------------------------
# Geo helpers (flat-earth approximation -- fine at this scale, ~6km box)
# ---------------------------------------------------------------------------

def meters_to_latlon_offset(lat0, dx_m, dy_m):
    """dx_m = east offset, dy_m = north offset, from point at lat0."""
    m_per_deg_lat = 111320.0
    m_per_deg_lon = 111320.0 * math.cos(math.radians(lat0))
    dlat = dy_m / m_per_deg_lat
    dlon = dx_m / m_per_deg_lon
    return dlat, dlon


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def random_point_near(lat0, lon0, radius_m, rng):
    ang = rng.uniform(0, 2 * math.pi)
    r = rng.uniform(0, radius_m)
    dx, dy = r * math.cos(ang), r * math.sin(ang)
    dlat, dlon = meters_to_latlon_offset(lat0, dx, dy)
    return lat0 + dlat, lon0 + dlon


# ---------------------------------------------------------------------------
# Topology generation: one DT's pole tree, via correlated random walk branches
# ---------------------------------------------------------------------------

def sample_poles_per_dt(rng):
    # Skewed distribution around the median, clipped to [min, max]
    n = int(rng.lognormvariate(math.log(POLES_PER_DT_MEDIAN), 0.6))
    return max(POLES_PER_DT_MIN, min(POLES_PER_DT_MAX, n))


def generate_dt_topology(dt_lat, dt_lon, target_n_poles, rng):
    """
    Returns a list of pole dicts (with lat, lon, seq_on_line, parent_pole_id
    as LOCAL indices into this list -- caller assigns real IDs), representing
    a tree of 1-5 road-like branches rooted at the transformer.
    """
    n_branches = rng.randint(*BRANCH_COUNT_RANGE)
    poles = []  # each: {lat, lon, seq_on_line, parent_idx (local, -1 = DT root)}

    # Give each branch a rough share of the pole budget
    weights = [rng.uniform(0.5, 1.5) for _ in range(n_branches)]
    total_w = sum(weights)
    branch_targets = [max(3, round(target_n_poles * w / total_w)) for w in weights]

    for b in range(n_branches):
        heading = rng.uniform(0, 2 * math.pi)
        cur_lat, cur_lon = dt_lat, dt_lon
        branch_len_m = 0.0
        seq = 0
        parent_local_idx = -1  # -1 means "connects directly to the DT"
        # Occasionally branch off an existing pole in an EARLIER branch, to
        # get real forking topology rather than N independent radial spokes.
        if b > 0 and poles and rng.random() < 0.5:
            fork_idx = rng.randrange(len(poles))
            cur_lat, cur_lon = poles[fork_idx]["lat"], poles[fork_idx]["lon"]
            parent_local_idx = fork_idx

        n_target = branch_targets[b]
        while seq < n_target and branch_len_m < MAX_BRANCH_LENGTH_M:
            heading += rng.uniform(-0.4, 0.4)  # gentle turns -> road-like path
            step = rng.uniform(MIN_POLE_SPACING_M, MAX_POLE_SPACING_M)
            dx, dy = step * math.cos(heading), step * math.sin(heading)
            dlat, dlon = meters_to_latlon_offset(cur_lat, dx, dy)
            cur_lat, cur_lon = cur_lat + dlat, cur_lon + dlon
            branch_len_m += step
            seq += 1

            poles.append({
                "lat": cur_lat,
                "lon": cur_lon,
                "seq_on_line": seq,
                "parent_idx": parent_local_idx,
            })
            parent_local_idx = len(poles) - 1  # next pole's parent is this one

    return poles[:target_n_poles] if len(poles) > target_n_poles else poles


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------

def generate(seed, n_substations, feeders_per_sub, dts_per_feeder, out_dir):
    rng = random.Random(seed)
    os.makedirs(out_dir, exist_ok=True)

    substations, feeders, transformers, poles, ground_truth = [], [], [], [], []

    global_dt_counter = 0
    global_pole_counter = 0

    for si in range(1, n_substations + 1):
        sub_id = f"SS-{si:02d}"
        sub_lat, sub_lon = random_point_near(CITY_CENTER_LAT, CITY_CENTER_LON,
                                              SUBDIVISION_SPREAD_M, rng)
        substations.append({"substation_id": sub_id, "lat": sub_lat, "lon": sub_lon})
        sub_pincode = rng.choice(PINCODES)

        for fi in range(1, feeders_per_sub + 1):
            feeder_id = f"F-{si:02d}-{fi:02d}"
            feeder_lat, feeder_lon = random_point_near(sub_lat, sub_lon, 2000, rng)
            feeders.append({"feeder_id": feeder_id, "substation_id": sub_id,
                             "lat": feeder_lat, "lon": feeder_lon})

            for di in range(dts_per_feeder):
                global_dt_counter += 1
                dt_id = f"D-{global_dt_counter:04d}"
                dt_lat, dt_lon = random_point_near(feeder_lat, feeder_lon, 800, rng)
                n_poles_target = sample_poles_per_dt(rng)

                local_poles = generate_dt_topology(dt_lat, dt_lon, n_poles_target, rng)
                capacity_kva = rng.choice([100, 160, 250, 400])
                households = int(len(local_poles) * rng.uniform(2.2, 3.4))

                transformers.append({
                    "dt_id": dt_id, "feeder_id": feeder_id,
                    "lat": dt_lat, "lon": dt_lon,
                    "capacity_kva": capacity_kva, "households_served": households,
                })

                ward = f"W-{rng.randint(1, 120):03d}"
                pincode = sub_pincode

                local_to_global = {}
                for local_idx, p in enumerate(local_poles):
                    global_pole_counter += 1
                    pole_id = f"P-{global_pole_counter:06d}"
                    local_to_global[local_idx] = pole_id

                for local_idx, p in enumerate(local_poles):
                    pole_id = local_to_global[local_idx]
                    parent_idx = p["parent_idx"]
                    parent_pole_id = local_to_global[parent_idx] if parent_idx != -1 else None
                    device_id = f"KSPDB-SD{si:02d}-{dt_id}-{global_pole_counter}" \
                        if False else f"KSPDB-SD{si:02d}-{dt_id}-{local_idx+1:04d}"

                    pole_row = {
                        "pole_id": pole_id,
                        "lat": round(p["lat"], 6),
                        "lon": round(p["lon"], 6),
                        "feeder_id": feeder_id,
                        "dt_id": dt_id,
                        "seq_on_line": p["seq_on_line"],
                        "parent_pole_id": parent_pole_id,
                        "pole_type": rng.choice(POLE_TYPES),
                        "ward": ward,
                        "pincode": pincode,
                        "device_id": device_id,
                    }
                    poles.append(pole_row)
                    ground_truth.append({
                        "pole_id": pole_id,
                        "true_seq_on_line": p["seq_on_line"],
                        "true_parent_pole_id": parent_pole_id,
                        "dt_id": dt_id,
                    })

    # ---- Corrupt a COPY of the registry to match the spec's dirtiness ----
    dt_ids = sorted({t["dt_id"] for t in transformers})
    n_missing_topo = round(len(dt_ids) * MISSING_TOPOLOGY_DT_FRACTION)
    dts_missing_topology = set(rng.sample(dt_ids, n_missing_topo))

    for row in poles:
        if row["dt_id"] in dts_missing_topology:
            row["seq_on_line"] = None
            row["parent_pole_id"] = None

    n_poles = len(poles)
    missing_device_idx = set(rng.sample(range(n_poles), round(n_poles * MISSING_DEVICE_ID_FRACTION)))
    missing_pincode_idx = set(rng.sample(range(n_poles), round(n_poles * MISSING_PINCODE_FRACTION)))
    for i, row in enumerate(poles):
        if i in missing_device_idx:
            row["device_id"] = None
        if i in missing_pincode_idx:
            row["pincode"] = None

    return substations, feeders, transformers, poles, ground_truth, dts_missing_topology


def write_csvs(out_dir, substations, feeders, transformers, poles, ground_truth):
    def _write(name, rows, fields):
        with open(os.path.join(out_dir, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    _write("substations.csv", substations, ["substation_id", "lat", "lon"])
    _write("feeders.csv", feeders, ["feeder_id", "substation_id", "lat", "lon"])
    _write("transformer_registry.csv", transformers,
           ["dt_id", "feeder_id", "lat", "lon", "capacity_kva", "households_served"])
    _write("pole_registry.csv", poles,
           ["pole_id", "lat", "lon", "feeder_id", "dt_id", "seq_on_line",
            "parent_pole_id", "pole_type", "ward", "pincode", "device_id"])
    # Ground truth: NOT part of the "given" data. Keep it out of the app's
    # data path entirely -- use it only from a validation/eval script.
    _write("ground_truth_topology.csv", ground_truth,
           ["pole_id", "true_seq_on_line", "true_parent_pole_id", "dt_id"])


def load_sqlite(db_path, substations, feeders, transformers, poles, ground_truth, seed, dts_missing_topology):
    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()

    cur.execute("""CREATE TABLE substations (
        substation_id TEXT PRIMARY KEY, lat REAL, lon REAL)""")
    cur.execute("""CREATE TABLE feeders (
        feeder_id TEXT PRIMARY KEY, substation_id TEXT, lat REAL, lon REAL)""")
    cur.execute("""CREATE TABLE transformer_registry (
        dt_id TEXT PRIMARY KEY, feeder_id TEXT, lat REAL, lon REAL,
        capacity_kva INTEGER, households_served INTEGER)""")
    cur.execute("""CREATE TABLE pole_registry (
        pole_id TEXT PRIMARY KEY, lat REAL, lon REAL, feeder_id TEXT, dt_id TEXT,
        seq_on_line INTEGER, parent_pole_id TEXT, pole_type TEXT, ward TEXT,
        pincode TEXT, device_id TEXT)""")
    cur.execute("""CREATE INDEX idx_pole_dt ON pole_registry(dt_id)""")
    cur.execute("""CREATE INDEX idx_pole_device ON pole_registry(device_id)""")

    # Ground truth lives in its own table -- your ingest/detection code has
    # no legitimate reason to ever query this table. It's for an offline
    # eval script only.
    cur.execute("""CREATE TABLE ground_truth_topology (
        pole_id TEXT PRIMARY KEY, true_seq_on_line INTEGER,
        true_parent_pole_id TEXT, dt_id TEXT)""")
    cur.execute("""CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)""")

    cur.executemany("INSERT INTO substations VALUES (:substation_id,:lat,:lon)", substations)
    cur.executemany("INSERT INTO feeders VALUES (:feeder_id,:substation_id,:lat,:lon)", feeders)
    cur.executemany("""INSERT INTO transformer_registry VALUES
        (:dt_id,:feeder_id,:lat,:lon,:capacity_kva,:households_served)""", transformers)
    cur.executemany("""INSERT INTO pole_registry VALUES
        (:pole_id,:lat,:lon,:feeder_id,:dt_id,:seq_on_line,:parent_pole_id,
         :pole_type,:ward,:pincode,:device_id)""", poles)
    cur.executemany("""INSERT INTO ground_truth_topology VALUES
        (:pole_id,:true_seq_on_line,:true_parent_pole_id,:dt_id)""", ground_truth)

    cur.executemany("INSERT INTO meta VALUES (?,?)", [
        ("seed", str(seed)),
        ("n_substations", str(len(substations))),
        ("n_feeders", str(len(feeders))),
        ("n_dts", str(len(transformers))),
        ("n_poles", str(len(poles))),
        ("n_dts_missing_topology", str(len(dts_missing_topology))),
    ])

    con.commit()
    con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n-substations", type=int, default=N_SUBSTATIONS)
    ap.add_argument("--feeders-per-sub", type=int, default=FEEDERS_PER_SUBSTATION)
    ap.add_argument("--dts-per-feeder", type=int, default=DTS_PER_FEEDER)
    ap.add_argument("--out-dir", type=str, default=".")
    ap.add_argument("--db", type=str, default="data.db")
    args = ap.parse_args()

    substations, feeders, transformers, poles, ground_truth, dts_missing = generate(
        args.seed, args.n_substations, args.feeders_per_sub, args.dts_per_feeder, args.out_dir)

    write_csvs(args.out_dir, substations, feeders, transformers, poles, ground_truth)
    load_sqlite(os.path.join(args.out_dir, args.db),
                substations, feeders, transformers, poles, ground_truth,
                args.seed, dts_missing)

    n_no_device = sum(1 for p in poles if p["device_id"] is None)
    n_no_pincode = sum(1 for p in poles if p["pincode"] is None)
    n_no_topo = sum(1 for p in poles if p["seq_on_line"] is None)

    print(f"Substations: {len(substations)}")
    print(f"Feeders:     {len(feeders)}")
    print(f"DTs:         {len(transformers)}  ({len(dts_missing)} missing topology, "
          f"{len(dts_missing)/len(transformers):.0%})")
    print(f"Poles:       {len(poles)}")
    print(f"  missing device_id: {n_no_device} ({n_no_device/len(poles):.1%})")
    print(f"  missing pincode:   {n_no_pincode} ({n_no_pincode/len(poles):.1%})")
    print(f"  missing topology:  {n_no_topo} ({n_no_topo/len(poles):.1%})")
    print(f"\nWrote CSVs + {args.db} to {os.path.abspath(args.out_dir)}")


if __name__ == "__main__":
    main()