"""HTTP-level tests for the connectivity quality check pages."""
from __future__ import annotations

import io

from tests.conftest import sample_bytes


def _upload(client, name):
    return client.post(
        "/upload",
        data={"osm_file": (io.BytesIO(sample_bytes(name)), name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _seed(client):
    for name in [
        "01_base_extract.osm",
        "02_missing_node.osm",
        "03_fill_gap_and_move_shared.osc",
        "04_delete_shared_node.osc",
        "06_way_reroute.osc",
        "08_same_version_conflict.osc",   # rejected, must not be selectable
    ]:
        _upload(client, name)


def _add_pair(client, start, end, label=""):
    return client.post(
        "/connectivity/pairs",
        data={"start_node_id": str(start), "end_node_id": str(end), "label": label},
        follow_redirects=True,
    )


def test_connectivity_page_lists_only_committed_batches(client):
    _seed(client)
    body = client.get("/connectivity").data.decode()
    assert "连通性质检" in body
    # Rejected batch is not offered for either side of the comparison.
    form = body.split("生成质检报告")[1].split("规则修订登记")[0]
    assert "08_same_version_conflict.osc" not in form
    assert "01_base_extract.osm" in form


def test_pairs_can_be_added_listed_and_deleted(client):
    resp = _add_pair(client, 1, 5, "主线")
    body = resp.data.decode()
    assert "已保存" in body and "主线" in body

    # Duplicate and non-positive ids are refused with a visible message.
    assert "已经保存" in _add_pair(client, 1, 5).data.decode()
    assert "正整数" in _add_pair(client, 0, 5).data.decode()

    assert 'action="/connectivity/pairs/1/delete"' in body
    resp = client.post("/connectivity/pairs/1/delete", follow_redirects=True)
    assert "已删除" in resp.data.decode()
    assert "主线" not in resp.data.decode()


def test_pair_cap_is_enforced_over_http(client):
    for i in range(20):
        assert "已保存" in _add_pair(client, i + 1, i + 100).data.decode()
    resp = _add_pair(client, 500, 600)
    assert "最多保存 20" in resp.data.decode()


def test_report_generation_and_classification_page(client):
    _seed(client)
    _add_pair(client, 1, 5, "跨区主路")
    _add_pair(client, 9, 5, "缺端点")

    resp = client.post(
        "/connectivity/reports",
        data={"from_batch_id": "1", "to_batch_id": "4"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    body = resp.data.decode()
    assert "质检报告 #1" in body
    assert "新增断连" in body and "跨区主路" in body
    assert "端点缺失" in body and "缺端点" in body
    # Rule version and both batches are recorded on the report.
    assert "connectivity-v1" in body
    assert "01_base_extract.osm" in body and "04_delete_shared_node.osc" in body
    # 1->4 deletes shared n4: the n1..n5 pair is a new disconnection.
    assert ">1<" in body


def test_report_rejects_rejected_batch_and_same_batch(client):
    _seed(client)
    _add_pair(client, 1, 5)
    body = client.post(
        "/connectivity/reports",
        data={"from_batch_id": "1", "to_batch_id": "6"},
        follow_redirects=True,
    ).data.decode()
    assert "只有已提交批次" in body

    body = client.post(
        "/connectivity/reports",
        data={"from_batch_id": "1", "to_batch_id": "1"},
        follow_redirects=True,
    ).data.decode()
    assert "两个不同" in body


def test_report_requires_saved_pairs(client):
    _seed(client)
    body = client.post(
        "/connectivity/reports",
        data={"from_batch_id": "1", "to_batch_id": "2"},
        follow_redirects=True,
    ).data.decode()
    assert "尚未保存任何起终点" in body


def test_rule_revision_marks_old_report_stale_on_page(client):
    _seed(client)
    _add_pair(client, 1, 5)
    client.post(
        "/connectivity/reports",
        data={"from_batch_id": "1", "to_batch_id": "5"},
        follow_redirects=True,
    )
    assert "已过期" not in client.get("/connectivity/reports/1").data.decode()

    resp = client.post(
        "/connectivity/rule-version",
        data={"rule_version": "connectivity-v2"},
        follow_redirects=True,
    )
    assert "已登记" in resp.data.decode()
    assert "已过期" in client.get("/connectivity/reports/1").data.decode()

    listing = client.get("/connectivity").data.decode()
    assert "已过期" in listing
    # The old report is retained, not deleted.
    assert "/connectivity/reports/1" in listing


def test_unknown_report_404(client):
    assert client.get("/connectivity/reports/999").status_code == 404
