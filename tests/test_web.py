"""HTTP-level tests: upload flow, pages, GeoJSON download, rejection handling."""
from __future__ import annotations

import io
import json

from tests.conftest import sample_bytes


def _upload(client, name):
    # Werkzeug 3 only treats file-like objects as multipart files; wrap the
    # raw bytes in BytesIO.
    return client.post(
        "/upload",
        data={"osm_file": (io.BytesIO(sample_bytes(name)), name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_index_empty(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "入仓批次".encode() in resp.data


def test_upload_commits_and_detail_page_lists_roads_and_pending(client):
    resp = _upload(client, "01_base_extract.osm")
    assert resp.status_code == 200
    assert "已整体生效".encode() in resp.data

    resp = _upload(client, "02_missing_node.osm")
    body = resp.data.decode()
    assert "待补全小路" in body or "102" in body

    # Pending w102 visible on detail page, missing node id shown.
    assert "9" in body
    listing = client.get("/").data.decode()
    assert "01_base_extract.osm" in listing and "已提交" in listing


def test_geojson_download_is_valid_feature_collection(client):
    _upload(client, "01_base_extract.osm")
    resp = client.get("/batches/1/geojson")
    assert resp.status_code == 200
    assert resp.mimetype == "application/geo+json"
    assert "attachment" in resp.headers.get("Content-Disposition", "")
    data = json.loads(resp.data)
    assert data["type"] == "FeatureCollection"
    assert sorted(f["properties"]["osm_way_id"] for f in data["features"]) == [100, 101]
    for feature in data["features"]:
        assert feature["geometry"]["type"] == "LineString"


def test_rejected_batch_shows_reason_and_blocks_export(client):
    _upload(client, "01_base_extract.osm")
    resp = _upload(client, "08_same_version_conflict.osc")
    body = resp.data.decode()
    assert "批次被拒绝" in body and "同版异内容" in body

    detail = client.get("/batches/2").data.decode()
    assert "已拒绝" in detail
    resp = client.get("/batches/2/geojson")
    assert resp.status_code == 409


def test_bad_extension_and_missing_file_rejected(client):
    resp = client.post(
        "/upload",
        data={"osm_file": (io.BytesIO(b"not osm"), "evil.txt")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "仅接受 .osm / .osc" in resp.data.decode()

    resp = client.post("/upload", data={}, follow_redirects=True)
    assert "未选择文件" in resp.data.decode()


def test_relation_upload_rejected_over_http(client):
    resp = _upload(client, "10_relation_rejected.osm")
    assert "relation" in resp.data.decode()
    listing = client.get("/").data.decode()
    assert "已拒绝" in listing


def test_unknown_batch_404(client):
    assert client.get("/batches/999").status_code == 404
    assert client.get("/batches/999/geojson").status_code == 404


def test_health(client):
    assert client.get("/health").get_json() == {"status": "ok"}


def test_reupload_same_file_is_idempotent(client):
    _upload(client, "01_base_extract.osm")
    resp = _upload(client, "01_base_extract.osm")
    assert "跳过重复同版 8" in resp.data.decode()  # 5 nodes + 3 ways
    data1 = client.get("/batches/1/geojson").data
    data2 = client.get("/batches/1/geojson").data
    assert data1 == data2
