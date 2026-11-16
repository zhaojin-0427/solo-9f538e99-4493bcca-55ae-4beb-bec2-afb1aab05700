"""End-to-end behaviour tests driven through the provided sample files."""
from __future__ import annotations

import io
import json

import pytest

from app.ingestion import BatchRejected, ingest_stream
from app.osm_parser import OSMParseError
from app.roads import build_road_layer, road_geojson


def ingest(conn, name):
    return ingest_stream(conn, name, io.BytesIO(conftest_sample(name)))


def conftest_sample(name):
    # Late import so the helper in conftest stays the single source of truth.
    from tests.conftest import sample_bytes
    return sample_bytes(name)


def _road_ids(roads):
    return sorted(r.way_id for r in roads)


def _pending_ids(pending):
    return sorted(p.way_id for p in pending)


def test_01_base_batch_builds_two_highway_roads(conn):
    r = ingest(conn, "01_base_extract.osm")
    assert (r.inserted_nodes, r.inserted_ways) == (5, 3)

    roads, pending = build_road_layer(conn, r.batch_id)
    assert _road_ids(roads) == [100, 101]          # w200 has no highway tag
    assert pending == []

    # Ordered node geometry, (lon, lat) GeoJSON order.
    w100 = next(x for x in roads if x.way_id == 100)
    assert w100.coordinates == [
        [121.47370, 31.23040],
        [121.47450, 31.23120],
        [121.47530, 31.23200],
    ]
    gj = road_geojson(roads)
    assert gj["type"] == "FeatureCollection"
    feat = next(f for f in gj["features"] if f["properties"]["osm_way_id"] == 101)
    assert feat["geometry"]["type"] == "LineString"
    assert feat["properties"]["highway"] == "tertiary"


def test_02_missing_node_makes_whole_way_pending_and_duplicate_skipped(conn):
    b1 = ingest(conn, "01_base_extract.osm")
    b2 = ingest(conn, "02_missing_node.osm")
    assert b2.duplicates == 1            # w101 v1 re-uploaded identically
    assert b2.inserted_ways == 1         # only w102 is new

    roads, pending = build_road_layer(conn, b2.batch_id)
    assert _road_ids(roads) == [100, 101]
    assert _pending_ids(pending) == [102]
    p = pending[0]
    assert p.missing_node_ids == [9]
    # Pending ways must not leak into the export.
    assert 102 not in [f["properties"]["osm_way_id"] for f in road_geojson(roads)["features"]]

    # Earlier batch snapshot is unaffected by batch 2.
    roads_b1, pending_b1 = build_road_layer(conn, b1.batch_id)
    assert _road_ids(roads_b1) == [100, 101]
    assert pending_b1 == []


def test_03_fill_gap_restores_way_and_shared_node_move_propagates(conn):
    ingest(conn, "01_base_extract.osm")
    b2 = ingest(conn, "02_missing_node.osm")
    b3 = ingest(conn, "03_fill_gap_and_move_shared.osc")

    roads_b2, _ = build_road_layer(conn, b2.batch_id)
    roads_b3, pending_b3 = build_road_layer(conn, b3.batch_id)
    assert pending_b3 == []
    assert _road_ids(roads_b3) == [100, 101, 102]

    # Shared node n3 moved: both ways geometry changes although neither was
    # re-uploaded; old batch 2 geometry keeps the old n3 coordinate.
    old_n3 = [121.47530, 31.23200]
    new_n3 = [121.47565, 31.23225]
    w100_b2 = next(x for x in roads_b2 if x.way_id == 100)
    w100_b3 = next(x for x in roads_b3 if x.way_id == 100)
    assert old_n3 in w100_b2.coordinates and new_n3 not in w100_b2.coordinates
    assert new_n3 in w100_b3.coordinates

    # Historical immutability: re-reading batch 1 still shows original data.
    roads_b1, _ = build_road_layer(conn, 1)
    w100_b1 = next(x for x in roads_b1 if x.way_id == 100)
    assert old_n3 in w100_b1.coordinates


def test_04_delete_shared_node_pulls_way_offline_but_keeps_versions(conn):
    ingest(conn, "01_base_extract.osm")
    b4 = ingest(conn, "04_delete_shared_node.osc")

    rows = conn.execute(
        "SELECT version, visible FROM object_versions "
        "WHERE object_type='node' AND object_id=4 ORDER BY version"
    ).fetchall()
    assert [(r["version"], r["visible"]) for r in rows] == [(1, 1), (2, 0)]

    roads, pending = build_road_layer(conn, b4.batch_id)
    assert _road_ids(roads) == [100]                 # w101 references deleted n4
    assert _pending_ids(pending) == [101]
    assert pending[0].missing_node_ids == [4]


def test_05_old_version_cannot_resurrect_deleted_object(conn):
    ingest(conn, "01_base_extract.osm")
    ingest(conn, "03_fill_gap_and_move_shared.osc")  # creates n3 v2 and n9 v1
    ingest(conn, "04_delete_shared_node.osc")
    b5 = ingest(conn, "05_old_version_ignored.osc")
    assert b5.ignored_old == 1      # n4 v1 ignored
    assert b5.duplicates == 1       # n3 v2 identical duplicate

    state = conn.execute(
        "SELECT visible FROM object_versions WHERE object_type='node' "
        "AND object_id=4 ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert state["visible"] == 0
    roads, pending = build_road_layer(conn, b5.batch_id)
    assert _pending_ids(pending) == [101]


def test_06_way_reroute_drops_old_node_influence(conn):
    ingest(conn, "01_base_extract.osm")
    ingest(conn, "02_missing_node.osm")
    ingest(conn, "03_fill_gap_and_move_shared.osc")
    ingest(conn, "04_delete_shared_node.osc")
    b5 = ingest(conn, "05_old_version_ignored.osc")
    b6 = ingest(conn, "06_way_reroute.osc")

    roads_b5, pending_b5 = build_road_layer(conn, b5.batch_id)
    assert _pending_ids(pending_b5) == [101]
    roads_b6, pending_b6 = build_road_layer(conn, b6.batch_id)
    assert pending_b6 == []
    w101 = next(x for x in roads_b6 if x.way_id == 101)
    assert w101.version == 2
    assert w101.coordinates == [
        [121.47565, 31.23225],   # n3 (moved)
        [121.47770, 31.23440],   # n6
        [121.47850, 31.23520],   # n9
    ]
    # Batch 5 view of w101 v1 is frozen even after the reroute in batch 6.
    assert 101 in _pending_ids(build_road_layer(conn, b5.batch_id)[1])


def test_07_out_of_order_changes_merge_by_identity(conn):
    ingest(conn, "01_base_extract.osm")
    b7 = ingest(conn, "07_out_of_order.osc")
    # n8 duplicate inside the file; n1 gets v2 and v3 (two inserts).
    assert b7.duplicates == 1

    n1 = conn.execute(
        "SELECT version FROM object_versions WHERE object_type='node' "
        "AND object_id=1 ORDER BY version"
    ).fetchall()
    assert [r["version"] for r in n1] == [1, 2, 3]
    current = conn.execute(
        "SELECT lat, lon FROM object_versions WHERE object_type='node' "
        "AND object_id=1 AND version=3"
    ).fetchone()
    assert (current["lat"], current["lon"]) == (31.23055, 121.47385)

    # Forward reference within the same batch resolves immediately.
    roads, pending = build_road_layer(conn, b7.batch_id)
    assert 103 in _road_ids(roads)
    w103 = next(x for x in roads if x.way_id == 103)
    assert len(w103.coordinates) == 2
    assert pending == []


def test_08_same_version_conflict_rejects_entire_batch_atomically(conn):
    ingest(conn, "01_base_extract.osm")
    before = conn.execute("SELECT COUNT(*) AS n FROM object_versions").fetchone()["n"]

    with pytest.raises(BatchRejected, match="同版异内容"):
        ingest(conn, "08_same_version_conflict.osc")

    # No partial rows: w104 must not exist and row count is unchanged.
    after = conn.execute("SELECT COUNT(*) AS n FROM object_versions").fetchone()["n"]
    assert after == before
    assert conn.execute(
        "SELECT 1 FROM object_versions WHERE object_type='way' AND object_id=104"
    ).fetchone() is None
    rejected = conn.execute(
        "SELECT status, error FROM batches ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert rejected["status"] == "rejected"
    assert "同版异内容" in rejected["error"]


def test_09_higher_version_revives_node_but_keeps_tombstone_history(conn):
    ingest(conn, "01_base_extract.osm")
    ingest(conn, "04_delete_shared_node.osc")
    b9 = ingest(conn, "09_recreate_node_higher_version.osc")

    rows = conn.execute(
        "SELECT version, visible FROM object_versions WHERE object_type='node' "
        "AND object_id=4 ORDER BY version"
    ).fetchall()
    assert [(r["version"], r["visible"]) for r in rows] == [(1, 1), (2, 0), (3, 1)]

    # w101 was rerouted only in sample 06 which this scenario did not load,
    # so it still references n4: with n4 revived, it becomes complete again.
    roads, pending = build_road_layer(conn, b9.batch_id)
    assert _road_ids(roads) == [100, 101]
    assert pending == []


def test_10_relation_file_rejected_entirely(conn):
    with pytest.raises(OSMParseError, match="relation"):
        ingest(conn, "10_relation_rejected.osm")
    assert conn.execute(
        "SELECT 1 FROM object_versions WHERE object_id=1000"
    ).fetchone() is None
    rejected = conn.execute(
        "SELECT status FROM batches ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert rejected["status"] == "rejected"


def test_highway_way_with_single_node_is_pending_until_it_has_two_points(conn):
    # A way whose refs resolve to fewer than two coordinates cannot form a
    # LineString and must be treated as pending as a whole.
    doc = b"""<?xml version="1.0"?>
    <osm version="0.6">
      <node id="1" version="1" lat="31.0" lon="121.0"/>
      <way id="50" version="1">
        <nd ref="1"/>
        <tag k="highway" v="path"/>
      </way>
    </osm>"""
    import io as _io
    r = ingest_stream(conn, "single.osm", _io.BytesIO(doc))
    roads, pending = build_road_layer(conn, r.batch_id)
    assert roads == []
    assert [p.way_id for p in pending] == [50]


def test_each_batch_export_is_immutable_after_later_writes(conn):
    ids = []
    for name in [
        "01_base_extract.osm",
        "02_missing_node.osm",
        "03_fill_gap_and_move_shared.osc",
        "04_delete_shared_node.osc",
        "06_way_reroute.osc",
        "07_out_of_order.osc",
    ]:
        ids.append(ingest(conn, name).batch_id)

    snapshots = []
    for bid in ids:
        roads, pending = build_road_layer(conn, bid)
        snapshots.append((
            _road_ids(roads),
            _pending_ids(pending),
            [r.coordinates for r in sorted(roads, key=lambda x: x.way_id)],
        ))
    # Recompute after more writes; every earlier snapshot must be identical.
    ingest(conn, "09_recreate_node_higher_version.osc")
    for bid, snap in zip(ids, snapshots):
        roads, pending = build_road_layer(conn, bid)
        current = (
            _road_ids(roads),
            _pending_ids(pending),
            [r.coordinates for r in sorted(roads, key=lambda x: x.way_id)],
        )
        assert current == snap
