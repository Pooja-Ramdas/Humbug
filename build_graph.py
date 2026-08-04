"""
build_graph.py

Builds a hierarchical network graph (substation -> feeder -> DT -> poles) from
data.db, for use by fault localization / grouping logic.

Key design decision: TRANSFORMERS ARE NODES, NOT EDGES.

An edge should represent a physical connection between two points (a span of
wire, or the notional link between hierarchy levels). A transformer is a
device that SITS at a point in the network -- it's the root of a pole-tree,
the thing a fault can happen AT (not just pass through), and the natural unit
for your "DT-level fallback" localization tier. Modeling it as a node lets you
ask graph questions directly: "give me every pole downstream of dt_id=D-0031"
is just a subtree/descendant query if the DT is a node. If it were an edge,
that same question becomes awkward (which edge? between which nodes?).

Graph shape:
    substation --(feeder_link)--> feeder --(dt_link)--> transformer --*--> poles

Two DIFFERENT edge types from a transformer down into its poles, which is the
whole point of this exercise:
    - "span"       : real recorded topology (seq_on_line/parent_pole_id known).
                      Edge goes parent -> child, weight = distance in meters.
                      These are the ~40% of DTs where you can do span-level
                      localization directly.
    - "membership" : topology unknown. A flat star edge straight from the DT
                      to every one of its poles, weight = None. This is not a
                      claim about wire routing -- it's just "this pole is
                      definitely fed by this DT, we don't know the order."
                      It's what lets DT-level fallback (Tier 4 from our
                      earlier discussion) work with zero extra logic: for any
                      pole, walk up to its nearest DT ancestor regardless of
                      edge type.

This split is intentional scaffolding for the topology-inference problem: your
Tier 2 (geometric inference) work later is literally "try to replace some
membership edges with inferred span edges, and see how often you're right
against ground_truth_topology.csv."

Usage:
    python3 build_graph.py --db out/data.db --out out/network.gpickle
"""

import argparse
import math
import pickle
import sqlite3
import networkx as nx


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def build_graph(db_path):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    G = nx.DiGraph()

    # --- substations ---
    for sub_id, lat, lon in cur.execute("SELECT substation_id, lat, lon FROM substations"):
        G.add_node(sub_id, type="substation", lat=lat, lon=lon)

    # --- feeders ---
    for feeder_id, sub_id, lat, lon in cur.execute(
            "SELECT feeder_id, substation_id, lat, lon FROM feeders"):
        G.add_node(feeder_id, type="feeder", lat=lat, lon=lon)
        sub_lat, sub_lon = G.nodes[sub_id]["lat"], G.nodes[sub_id]["lon"]
        G.add_edge(sub_id, feeder_id, edge_type="feeder_link",
                    weight_m=haversine_m(sub_lat, sub_lon, lat, lon))

    # --- transformers (DTs) : NODES ---
    for dt_id, feeder_id, lat, lon, kva, households in cur.execute(
            "SELECT dt_id, feeder_id, lat, lon, capacity_kva, households_served "
            "FROM transformer_registry"):
        G.add_node(dt_id, type="dt", lat=lat, lon=lon,
                    capacity_kva=kva, households_served=households)
        f_lat, f_lon = G.nodes[feeder_id]["lat"], G.nodes[feeder_id]["lon"]
        G.add_edge(feeder_id, dt_id, edge_type="dt_link",
                    weight_m=haversine_m(f_lat, f_lon, lat, lon))

    # --- poles : NODES, plus span/membership edges ---
    poles = list(cur.execute(
        "SELECT pole_id, lat, lon, dt_id, seq_on_line, parent_pole_id, "
        "pole_type, ward, pincode, device_id FROM pole_registry"))

    for pole_id, lat, lon, dt_id, seq, parent_id, ptype, ward, pincode, device_id in poles:
        G.add_node(pole_id, type="pole", lat=lat, lon=lon, dt_id=dt_id,
                    seq_on_line=seq, pole_type=ptype, ward=ward,
                    pincode=pincode, device_id=device_id,
                    has_device=device_id is not None)

    for pole_id, lat, lon, dt_id, seq, parent_id, ptype, ward, pincode, device_id in poles:
        dt_lat, dt_lon = G.nodes[dt_id]["lat"], G.nodes[dt_id]["lon"]

        if seq is None:
            # Topology unknown for this DT -> flat membership star from DT.
            G.add_edge(dt_id, pole_id, edge_type="membership", weight_m=None)
        else:
            if parent_id is None:
                # First pole on its branch -> connects directly to the DT,
                # and here we DO know the real distance/order, so it's a span.
                G.add_edge(dt_id, pole_id, edge_type="span",
                            weight_m=haversine_m(dt_lat, dt_lon, lat, lon))
            else:
                p_lat, p_lon = G.nodes[parent_id]["lat"], G.nodes[parent_id]["lon"]
                G.add_edge(parent_id, pole_id, edge_type="span",
                            weight_m=haversine_m(p_lat, p_lon, lat, lon))

    con.close()
    return G


def summarize(G):
    from collections import Counter
    node_types = Counter(nx.get_node_attributes(G, "type").values())
    edge_types = Counter(nx.get_edge_attributes(G, "edge_type").values())

    print("--- Node counts ---")
    for t, c in node_types.items():
        print(f"  {t}: {c}")

    print("--- Edge counts ---")
    for t, c in edge_types.items():
        print(f"  {t}: {c}")

    dt_nodes = [n for n, d in G.nodes(data=True) if d["type"] == "dt"]
    dts_with_span = set()
    for u, v, d in G.edges(data=True):
        if d["edge_type"] == "span" and G.nodes[u]["type"] == "dt":
            dts_with_span.add(u)
        if d["edge_type"] == "span" and G.nodes.get(v, {}).get("type") == "pole":
            pole_dt = G.nodes[v].get("dt_id")
            if pole_dt:
                dts_with_span.add(pole_dt)
    print(f"--- {len(dts_with_span)}/{len(dt_nodes)} DTs have real span topology "
          f"(rest are membership-only -> DT-level fallback territory)")

    # quick usage demo: everything downstream of one DT, regardless of edge type
    sample_dt = dt_nodes[0]
    downstream = nx.descendants(G, sample_dt)
    poles_downstream = [n for n in downstream if G.nodes[n]["type"] == "pole"]
    print(f"--- demo query: {len(poles_downstream)} poles downstream of {sample_dt} "
          f"(works identically whether topology is known or membership-only)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="out/data.db")
    ap.add_argument("--out", default="out/network.gpickle")
    args = ap.parse_args()

    G = build_graph(args.db)
    summarize(G)

    with open(args.out, "wb") as f:
        pickle.dump(G, f)
    print(f"\nSaved graph ({G.number_of_nodes()} nodes, {G.number_of_edges()} edges) to {args.out}")


if __name__ == "__main__":
    main()