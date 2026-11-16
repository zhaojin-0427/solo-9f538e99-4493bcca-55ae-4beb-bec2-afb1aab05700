"""Connectivity quality check between two committed batches."""
from __future__ import annotations

import io

import pytest

from app import connectivity
from app.connectivity import (
    CATEGORY_ENDPOINT_MISSING,
    CATEGORY_NEW_DISCONNECTION,
    CATEGORY_RECOVERED,
    CATEGORY_UNCHANGED,
    ConnectivityError,
)
from app.db import connect, init_db
from app.ingestion import ingest_stream
from tests.conftest import sample_bytes


def _ingest(conn, name):
    return ingest_stream(conn, name, io.BytesIO(sample_bytes(name))).batch_id


def _seed_standard(conn):
    """Batches 1..5: base, missing node, fill+move, delete shared node, reroute."""
    return [
        _ingest(conn, name) for name in [
            "01_base_extract.osm",
            "02_missing_node.osm",
            "03_fill_gap_and_move_shared.osc",
            "04_delete_shared_node.osc",
            "06_way_reroute.osc",
        ]
    ]


def _item(items, start, end):
    return next(i for i in items if i["start_node_id"] == start and i["end_node_id"] == end)


def _classify(conn, pairs, fb, tb):
    items, summary = connectivity.evaluate_pairs(conn, pairs, fb, tb)
    return {(i["start_node_id"], i["end_node_id"]): i["category"] for i in items}, summary


def _add(conn, start, end):
    connectivity.add_pair(conn, start, end)


# ---------------------------------------------------------------------------
# Graph rules
# ---------------------------------------------------------------------------


def test_edges_follow_adjacent_references_not_clique(conn):
    # w100 = n1-n2-n3 and w101 = n3-n4-n5.  n1 and n3 are adjacent (connected),
    # n1 and n4 are connected via the chain, but n2 and n4 are only connected
    # through n3.  Build a graph where naive "all pairs on one way" would give
    # a different answer: two ways sharing no node are separate components.
    doc = b"""<?xml version="1.0"?>
    <osm version="0.6">
      <node id="1" version="1" lat="31.0" lon="121.0"/>
      <node id="2" version="1" lat="31.1" lon="121.1"/>
      <node id="3" version="1" lat="31.2" lon="121.2"/>
      <node id="4" version="1" lat="31.3" lon="121.3"/>
      <node id="5" version="1" lat="31.4" lon="121.4"/>
      <way id="10" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/>
        <tag k="highway" v="residential"/></way>
      <way id="11" version="1"><nd ref="4"/><nd ref="5"/>
        <tag k="highway" v="residential"/></way>
    </osm>"""
    bid = ingest_stream(conn, "adjacent.osm", io.BytesIO(doc)).batch_id
    graph = connectivity.snapshot_graph(conn, bid)
    assert graph.components.connected(1, 3)
    assert graph.components.connected(1, 5) is False
    assert graph.components.connected(4, 5)


def test_pending_way_adds_no_edges_and_missing_endpoint_is_separate(conn):
    # w102 references missing n9: it contributes no edges.  But n5->n6 must
    # not become connected through w102 (n5, n6, n9 are its refs).
    _ingest(conn, "01_base_extract.osm")
    b2 = _ingest(conn, "02_missing_node.osm")
    _add(conn, 5, 6)
    _add(conn, 5, 9)

    items, summary = connectivity.evaluate_pairs(
        conn, connectivity.list_pairs(conn), 1, b2
    )
    assert _item(items, 5, 6)["category"] == CATEGORY_ENDPOINT_MISSING
    assert _item(items, 5, 6)["new"]["state"] == connectivity.STATE_INDETERMINATE
    assert summary["endpoint_missing_pairs"] == 2
    # Missing n9 is listed, and not counted as a disconnection / recovery.
    assert summary["new_disconnection"] == 0
    assert summary["recovered"] == 0
    assert 9 in summary["missing_node_ids_new"]
    assert 102 in summary["pending_ways_new"]


def test_new_disconnection_and_recovery_are_classified(conn):
    ids = _seed_standard(conn)
    for pair in [(1, 5), (3, 9), (4, 5), (1, 5)]:
        pass
    _add(conn, 1, 5)
    _add(conn, 2, 4)
    pairs = connectivity.list_pairs(conn)

    # Batch 1 -> 4: deleting shared n4 disconnects the two chains.
    cats, summary = _classify(conn, pairs, ids[0], ids[3])
    assert cats[(1, 5)] == CATEGORY_NEW_DISCONNECTION
    assert summary[CATEGORY_NEW_DISCONNECTION] == 1

    # Batch 4 -> 5: rerouting w101 reconnects n1 to n5.
    cats, summary = _classify(conn, pairs, ids[3], ids[4])
    assert cats[(1, 5)] == CATEGORY_RECOVERED
    assert summary[CATEGORY_RECOVERED] == 1

    # Batch 1 -> 5: no net change for n1..n5.
    cats, summary = _classify(conn, pairs, ids[0], ids[4])
    assert cats[(1, 5)] == CATEGORY_UNCHANGED
    assert summary[CATEGORY_UNCHANGED] >= 1


def test_deleted_endpoint_is_endpoint_missing_not_disconnection(conn):
    _ingest(conn, "01_base_extract.osm")
    b4 = _ingest(conn, "04_delete_shared_node.osc")
    _add(conn, 2, 4)
    items, summary = connectivity.evaluate_pairs(
        conn, connectivity.list_pairs(conn), 1, b4
    )
    item = _item(items, 2, 4)
    assert item["category"] == CATEGORY_ENDPOINT_MISSING
    assert item["new"]["state"] == connectivity.STATE_INDETERMINATE
    assert item["new"]["end_status"] == connectivity.ENDPOINT_ABSENT
    assert summary["new_disconnection"] == 0


def test_pending_only_endpoint_is_endpoint_missing(conn):
    # n6 is visible but only referenced by pending w102, so it cannot be
    # judged; a pair using it is listed separately, not as a disconnection.
    _ingest(conn, "01_base_extract.osm")
    b2 = _ingest(conn, "02_missing_node.osm")
    _add(conn, 5, 6)
    items, _ = connectivity.evaluate_pairs(conn, connectivity.list_pairs(conn), 1, b2)
    item = _item(items, 5, 6)
    assert item["category"] == CATEGORY_ENDPOINT_MISSING
    assert item["new"]["end_status"] == connectivity.ENDPOINT_PENDING_ONLY


def test_isolated_visible_node_is_judged_not_treated_as_pending(conn):
    # Batch 1 has a lone visible node 77 (referenced by nothing).  Batch 2 adds
    # a complete highway way n1-77.  Both endpoints exist in both snapshots,
    # so the pair is judged: disconnected -> connected = recovery.  A lone
    # node must not be silently treated as pending data.
    base = sample_bytes("01_base_extract.osm").decode()
    doc1 = base.replace(
        "</osm>",
        '<node id="77" version="1" lat="31.30" lon="121.30"/></osm>',
    ).encode()
    b1 = ingest_stream(conn, "with_lone.osm", io.BytesIO(doc1)).batch_id
    doc2 = b"""<?xml version="1.0"?>
    <osmChange version="0.6"><create>
      <way id="70" version="1"><nd ref="1"/><nd ref="77"/>
        <tag k="highway" v="path"/></way>
    </create></osmChange>"""
    b2 = ingest_stream(conn, "connect_lone.osc", io.BytesIO(doc2)).batch_id

    _add(conn, 1, 77)
    items, summary = connectivity.evaluate_pairs(
        conn, connectivity.list_pairs(conn), b1, b2
    )
    item = _item(items, 1, 77)
    assert item["old"]["end_status"] == connectivity.ENDPOINT_ISOLATED
    assert item["old"]["state"] == connectivity.STATE_DISCONNECTED
    assert item["new"]["state"] == connectivity.STATE_CONNECTED
    assert item["category"] == CATEGORY_RECOVERED
    assert summary[CATEGORY_RECOVERED] == 1


def test_batches_must_be_committed_distinct_and_ordered(conn):
    _ingest(conn, "01_base_extract.osm")
    _ingest(conn, "02_missing_node.osm")
    try:
        ingest_stream(conn, "bad.osc", io.BytesIO(sample_bytes("08_same_version_conflict.osc")))
    except Exception:
        pass
    rejected_id = conn.execute(
        "SELECT MAX(id) AS id FROM batches WHERE status = 'rejected'"
    ).fetchone()["id"]
    _add(conn, 1, 5)
    pairs = connectivity.list_pairs(conn)

    with pytest.raises(ConnectivityError, match="状态"):
        connectivity.evaluate_pairs(conn, pairs, 1, rejected_id)
    with pytest.raises(ConnectivityError, match="不同"):
        connectivity.evaluate_pairs(conn, pairs, 1, 1)
    with pytest.raises(ConnectivityError, match="早于"):
        connectivity.evaluate_pairs(conn, pairs, 2, 1)
    with pytest.raises(ConnectivityError, match="不存在"):
        connectivity.evaluate_pairs(conn, pairs, 1, 999)


def test_rejected_batch_as_from_batch_is_refused(conn):
    # Put a rejected batch *before* a committed one so the status check (not
    # the ordering check) is what rejects it.
    try:
        ingest_stream(conn, "bad.osm", io.BytesIO(sample_bytes("10_relation_rejected.osm")))
    except Exception:
        pass
    _ingest(conn, "01_base_extract.osm")
    _ingest(conn, "02_missing_node.osm")
    _add(conn, 1, 5)
    pairs = connectivity.list_pairs(conn)

    with pytest.raises(ConnectivityError, match="状态"):
        connectivity.evaluate_pairs(conn, pairs, 1, 3)
    with pytest.raises(ConnectivityError, match="状态"):
        connectivity.create_report(conn, 1, 3)


def test_report_is_persisted_with_batches_rule_and_snapshot(conn):
    ids = _seed_standard(conn)
    _add(conn, 1, 5)
    _add(conn, 9, 5)
    report_id = connectivity.create_report(conn, ids[0], ids[3])

    report, items, summary = connectivity.load_report(conn, report_id)
    assert (report["from_batch_id"], report["to_batch_id"]) == (ids[0], ids[3])
    assert report["rule_version"] == connectivity.config.CONNECTIVITY_RULE_VERSION
    assert report["stale"] == 0
    assert summary[CATEGORY_NEW_DISCONNECTION] == 1
    assert summary["endpoint_missing_pairs"] == 1
    assert summary["to_pending"]["missing_node_ids"] == [4]
    assert summary["to_pending"]["pending_way_ids"] == [101]
    # Report rows are a frozen snapshot, including endpoint reasons.
    pair_items = {i["start_node_id"]: i for i in items}
    assert pair_items[1]["category"] == CATEGORY_NEW_DISCONNECTION
    # n9 only appears from batch 3 on, so in the 1 -> 4 comparison it is
    # absent at the start snapshot and present at the end snapshot; the pair
    # is still endpoint_missing and kept out of the verdict counts.
    assert pair_items[9]["old_endpoints"] == {
        "start": connectivity.ENDPOINT_ABSENT,
        "end": connectivity.ENDPOINT_PRESENT,
    }
    assert pair_items[9]["new_endpoints"] == {
        "start": connectivity.ENDPOINT_PRESENT,
        "end": connectivity.ENDPOINT_PRESENT,
    }
    assert pair_items[9]["category"] == CATEGORY_ENDPOINT_MISSING


def test_report_is_frozen_after_later_ingestion(conn):
    _ingest(conn, "01_base_extract.osm")
    b2 = _ingest(conn, "02_missing_node.osm")
    _add(conn, 1, 5)
    report_id = connectivity.create_report(conn, 1, b2)
    _, before, _ = connectivity.load_report(conn, report_id)

    _ingest(conn, "03_fill_gap_and_move_shared.osc")
    _ingest(conn, "04_delete_shared_node.osc")
    report, after, _ = connectivity.load_report(conn, report_id)

    assert report["stale"] == 0  # batches changing does not stale a report
    assert [(i["old_state"], i["new_state"], i["category"]) for i in after] == \
           [(i["old_state"], i["new_state"], i["category"]) for i in before]
    # A fresh report for the same batches shows the same historical result.
    fresh_id = connectivity.create_report(conn, 1, b2)
    _, fresh, _ = connectivity.load_report(conn, fresh_id)
    assert [(i["category"]) for i in fresh] == [(i["category"]) for i in before]


def test_uncommitted_batch_cannot_produce_a_report(conn):
    _ingest(conn, "01_base_extract.osm")
    try:
        ingest_stream(conn, "bad.osc", io.BytesIO(sample_bytes("08_same_version_conflict.osc")))
    except Exception:
        pass
    rejected = conn.execute("SELECT MAX(id) AS id FROM batches").fetchone()["id"]
    _add(conn, 1, 5)
    with pytest.raises(ConnectivityError):
        connectivity.create_report(conn, 1, rejected)
    # No report row was written by the failed attempt.
    assert conn.execute("SELECT COUNT(*) AS n FROM connectivity_reports").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Rule revisions and staleness
# ---------------------------------------------------------------------------


def test_rule_revision_keeps_old_report_but_marks_it_stale(conn, monkeypatch):
    _ingest(conn, "01_base_extract.osm")
    b2 = _ingest(conn, "02_missing_node.osm")
    _add(conn, 1, 5)
    old_report = connectivity.create_report(conn, 1, b2)
    _, before_items, _ = connectivity.load_report(conn, old_report)

    connectivity.register_rule_version(conn, "connectivity-v2")

    report, items, _ = connectivity.load_report(conn, old_report)
    assert report["stale"] == 1
    assert "connectivity-v2" in report["stale_reason"]
    assert "connectivity-v1" in report["stale_reason"]
    # Old report content is preserved unchanged.
    assert [(i["category"]) for i in items] == [(i["category"]) for i in before_items]

    # Deploying code that implements v2 (simulated by the config bump): a new
    # report carries the newest revision and is current.
    monkeypatch.setattr(connectivity.config, "CONNECTIVITY_RULE_VERSION", "connectivity-v2")
    new_report = connectivity.create_report(conn, 1, b2)
    newest, _, _ = connectivity.load_report(conn, new_report)
    assert newest["rule_version"] == "connectivity-v2"
    assert newest["stale"] == 0


def test_restarting_an_older_build_does_not_revive_stale_reports(conn, db_path):
    _ingest(conn, "01_base_extract.osm")
    b2 = _ingest(conn, "02_missing_node.osm")
    _add(conn, 1, 5)
    report_id = connectivity.create_report(conn, 1, b2)
    connectivity.register_rule_version(conn, "connectivity-v2")
    conn.close()

    # Simulate redeploying the older code: it registers v1 (already known,
    # keeps its id) and must NOT clear the stale flag on the v1 report.
    init_db(db_path)
    check = connect(db_path)
    report, _, _ = connectivity.load_report(check, report_id)
    assert report["stale"] == 1
    assert connectivity.current_rule_version(check)["version"] == "connectivity-v2"
    check.close()


def test_new_report_under_old_rules_is_born_stale_when_revision_raced(conn):
    """A revision committed while an old-rule analysis runs stales its output.

    The revision is registered inside the analysis window but directly on the
    same open transaction, which is exactly what the report transaction would
    observe if another connection's revision had committed before its final
    check.  The report must be stored stale (audit only), never current.
    """
    _ingest(conn, "01_base_extract.osm")
    b2 = _ingest(conn, "02_missing_node.osm")
    _add(conn, 1, 5)

    real_evaluate = connectivity.evaluate_pairs

    def racy_evaluate(c, pairs, fb, tb):
        result = real_evaluate(c, pairs, fb, tb)
        c.execute(
            "INSERT OR IGNORE INTO rule_versions (version) VALUES (?)",
            ("connectivity-v9",),
        )
        return result

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(connectivity, "evaluate_pairs", racy_evaluate)
        report_id = connectivity.create_report(conn, 1, b2)

    report, items, _ = connectivity.load_report(conn, report_id)
    assert report["rule_version"] == connectivity.config.CONNECTIVITY_RULE_VERSION
    assert report["stale"] == 1
    assert "connectivity-v9" in report["stale_reason"]
    assert items  # results still stored for audit


def test_concurrent_revision_lands_after_report_and_stales_it(conn, db_path):
    """A revision racing with analysis serializes after it and stales its output.

    ``BEGIN IMMEDIATE`` holds the write lock for the whole analysis, so a
    concurrent revision waits, commits after the report, and marks it stale.
    Whichever order wins, the old-rule report never remains the current
    conclusion.
    """
    import threading

    _ingest(conn, "01_base_extract.osm")
    b2 = _ingest(conn, "02_missing_node.osm")
    _add(conn, 1, 5)

    analyzing = threading.Event()
    real_evaluate = connectivity.evaluate_pairs

    def slow_evaluate(c, pairs, fb, tb):
        result = real_evaluate(c, pairs, fb, tb)
        analyzing.set()
        threading.Event().wait(0.3)  # leave the report transaction open
        return result

    result = {}
    other = connect(db_path)
    other.execute("PRAGMA busy_timeout = 60000")

    def run_report():
        result["report_id"] = connectivity.create_report(conn, 1, b2)

    revision_done = threading.Event()

    def run_revision():
        analyzing.wait(5)
        connectivity.register_rule_version(other, "connectivity-v9")
        revision_done.set()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(connectivity, "evaluate_pairs", slow_evaluate)
        report_thread = threading.Thread(target=run_report)
        revision_thread = threading.Thread(target=run_revision)
        report_thread.start()
        revision_thread.start()
        report_thread.join(30)
        revision_thread.join(60)

    other.close()
    assert revision_done.is_set()
    assert result["report_id"]

    report, _, _ = connectivity.load_report(conn, result["report_id"])
    assert report["stale"] == 1
    assert "connectivity-v9" in report["stale_reason"]


def test_pairs_are_capped_at_twenty(conn):
    for i in range(connectivity.config.MAX_WATCH_PAIRS):
        _add(conn, i + 1, i + 100)
    assert len(connectivity.list_pairs(conn)) == 20
    with pytest.raises(ConnectivityError, match="最多保存 20"):
        _add(conn, 500, 600)
    assert len(connectivity.list_pairs(conn)) == 20


def test_duplicate_pair_rejected(conn):
    _add(conn, 1, 5)
    with pytest.raises(ConnectivityError, match="已经保存"):
        _add(conn, 1, 5)


def test_invalid_pair_ids_rejected(conn):
    with pytest.raises(ConnectivityError):
        _add(conn, 0, 5)
    with pytest.raises(ConnectivityError):
        _add(conn, "abc", 5)
    with pytest.raises(ConnectivityError, match="同一个"):
        _add(conn, 5, 5)


def test_report_requires_saved_pairs(conn):
    _ingest(conn, "01_base_extract.osm")
    b2 = _ingest(conn, "02_missing_node.osm")
    with pytest.raises(ConnectivityError, match="起终点"):
        connectivity.create_report(conn, 1, b2)


def test_connectivity_never_changes_batch_geojson(conn):
    """Running the quality check must not alter the original GeoJSON snapshot."""
    from app.roads import build_road_layer, road_geojson
    ids = _seed_standard(conn)
    _add(conn, 1, 5)
    before = {bid: road_geojson(build_road_layer(conn, bid)[0]) for bid in ids}

    connectivity.create_report(conn, ids[0], ids[3])
    connectivity.add_pair(conn, 3, 9)
    connectivity.register_rule_version(conn, "connectivity-v2")

    after = {bid: road_geojson(build_road_layer(conn, bid)[0]) for bid in ids}
    assert after == before

    # Even later ingestion leaves earlier batches' GeoJSON untouched.
    later = _ingest(conn, "09_recreate_node_higher_version.osc")
    assert road_geojson(build_road_layer(conn, later)[0]) != before[ids[0]]
    assert road_geojson(build_road_layer(conn, ids[0])[0]) == before[ids[0]]
