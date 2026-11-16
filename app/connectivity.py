"""Connectivity quality check between two committed batches.

Rule set (reported as ``config.CONNECTIVITY_RULE_VERSION``)
-----------------------------------------------------------
For each snapshot (the state as of a committed batch id) the connectivity
graph is built *only* from complete highway ways:

* a way counts only if it is visible, carries the ``highway`` tag and every
  ``<nd>`` reference resolves to a visible node with at least 2 points --
  exactly the ways exported in the road layer;
* edges connect *adjacent referenced nodes* in ``<nd>`` order; a way is a
  chain, not a clique, so two nodes far apart along the way are not directly
  connected;
* a pending way (any missing / deleted referenced node) contributes **no
  edges at all**; its missing node ids are recorded separately.  A pending
  way is never the basis for a disconnection or a recovery;
* a pair is connected when its start and end node lie in the same connected
  component of that graph.

Endpoint handling ("端点缺失单列"): a pair whose start or end node is deleted,
never uploaded, or only referenced by pending ways is listed in its own
"端点缺失" section with the reason and node id.  Such a pair is never counted
as a disconnection or a recovery, because connectivity cannot be judged
without both endpoints on the road network.  A visible node that is on no
road at all (and not merely waiting for a pending way) is a real lone
vertex, so it is still judged normally.

Only the remaining (judgeable) pairs are classified:

* ``new_disconnection`` (新增断连) -- connected before, not connected now;
* ``recovered`` (恢复连通) -- disconnected before, connected now;
* ``unchanged`` (未变化) -- the same verdict in both snapshots.

Every report stores the two batch ids, the rule version and a snapshot of
each pair's old/new state, so later uploads or rule revisions cannot rewrite
history.  A report produced under an older rule revision is flagged stale:
it is kept for the record but never presented as the current conclusion.
"""
from __future__ import annotations

import json

from . import config
from .roads import build_road_layer, current_objects

CATEGORY_NEW_DISCONNECTION = "new_disconnection"
CATEGORY_RECOVERED = "recovered"
CATEGORY_UNCHANGED = "unchanged"
# Not a comparison category: these pairs are listed separately and excluded
# from the three verdicts above.
CATEGORY_ENDPOINT_MISSING = "endpoint_missing"

CATEGORY_ORDER = (
    CATEGORY_NEW_DISCONNECTION,
    CATEGORY_RECOVERED,
    CATEGORY_UNCHANGED,
)

CATEGORY_LABELS = {
    CATEGORY_NEW_DISCONNECTION: "新增断连",
    CATEGORY_RECOVERED: "恢复连通",
    CATEGORY_UNCHANGED: "未变化",
    CATEGORY_ENDPOINT_MISSING: "端点缺失（单列，不参与判定）",
}

STATE_CONNECTED = "connected"
STATE_DISCONNECTED = "disconnected"
STATE_INDETERMINATE = "indeterminate"

STATE_LABELS = {
    STATE_CONNECTED: "连通",
    STATE_DISCONNECTED: "断连",
    STATE_INDETERMINATE: "端点缺失",
}

# Why a pair endpoint is not a graph vertex in a given snapshot.
ENDPOINT_PRESENT = "present"
ENDPOINT_ABSENT = "absent"          # never uploaded or deleted (tombstone)
ENDPOINT_PENDING_ONLY = "pending_only"  # visible but referenced only by
                                        # pending ways -> pending data
ENDPOINT_ISOLATED = "isolated"          # visible, on no complete or pending
                                        # way: a real lone graph vertex

ENDPOINT_LABELS = {
    ENDPOINT_PRESENT: "在道路上",
    ENDPOINT_ABSENT: "节点不存在/已删除",
    ENDPOINT_PENDING_ONLY: "仅由待补全 way 引用，无法判定",
    ENDPOINT_ISOLATED: "孤立节点（无道路连接）",
}


class ConnectivityError(ValueError):
    """Raised when a report cannot be produced (bad input, missing batch...)."""


# ---------------------------------------------------------------------------
# Graph construction
# ---------------------------------------------------------------------------


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict[int, int] = {}
        self._size: dict[int, int] = {}

    def add(self, x: int) -> None:
        if x not in self._parent:
            self._parent[x] = x
            self._size[x] = 1

    def find(self, x: int) -> int:
        parent = self._parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path compression
            parent[x], x = root, parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        self.add(a)
        self.add(b)
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self._size[ra] < self._size[rb]:
            ra, rb = rb, ra
        self._parent[rb] = ra
        self._size[ra] += self._size[rb]

    def connected(self, a: int, b: int) -> bool:
        if a not in self._parent or b not in self._parent:
            return False
        return self.find(a) == self.find(b)

    def component_size(self, x: int) -> int:
        return self._size.get(self.find(x), 0) if x in self._parent else 0


class SnapshotGraph:
    """Connectivity graph of one batch snapshot plus pending-way metadata."""

    def __init__(self, components, visible, road_nodes, missing_refs,
                 pending_refs, pending_ways):
        self.components = components
        # Visible nodes in the snapshot (either may be a pair endpoint).
        self.visible = visible
        # Nodes that belong to at least one complete highway way (graph vertices).
        self.road_nodes = road_nodes
        # Referenced-but-missing/deleted node ids (pending way endpoints).
        self.missing_node_ids = missing_refs
        # All node ids referenced by a pending way (visible or not).
        self.pending_refs = pending_refs
        self.pending_ways = pending_ways


def _refs_from_json(refs_json: str | None) -> list[int]:
    return json.loads(refs_json) if refs_json is not None else []


def _pending_snapshot(raw: str | None) -> dict:
    """Decode a stored pending-way snapshot, tolerating older/empty rows."""
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    return {
        "pending_way_ids": value.get("pending_way_ids", []),
        "missing_node_ids": value.get("missing_node_ids", []),
    }


def snapshot_graph(conn, batch_id: int) -> SnapshotGraph:
    """Build the connectivity graph as of a committed ``batch_id`` (read-only).

    Vertices are all *visible* nodes in the snapshot; edges come only from
    complete highway ways (adjacent ``<nd>`` refs).  A pending way adds no
    edges, so its already-visible nodes stay isolated.  Uses the same road
    layer reconstruction as the GeoJSON export, so the two can never disagree
    about which ways are complete.
    """
    roads, pending = build_road_layer(conn, batch_id)
    objects = current_objects(conn, batch_id)

    # Missing / deleted nodes referenced by pending ways.  A short way
    # (<2 points) with every node visible is also pending but has no missing
    # endpoint; it must not be recorded as a missing node id.
    missing_refs: set[int] = set()
    pending_refs: set[int] = set()
    for way in pending:
        way_missing = [
            ref for ref in way.refs
            if (objects.get(("node", ref)) is None
                or not objects[("node", ref)]["visible"])
        ]
        if way_missing:
            missing_refs.update(way_missing)
            # Only ways genuinely waiting for missing nodes make their other
            # nodes "pending data"; an incomplete way whose nodes are all
            # present is simply not a road yet.
            pending_refs.update(way.refs)

    uf = _UnionFind()
    visible: set[int] = set()
    for (obj_type, obj_id), row in objects.items():
        if obj_type == "node" and row["visible"]:
            visible.add(obj_id)
            uf.add(obj_id)

    road_nodes: set[int] = set()
    for road in roads:
        # build_road_layer only returns ways whose every nd resolves to a
        # visible node, and carries that version's authoritative nd sequence.
        node_ids = _refs_from_json(road.refs_json)
        for node_id in node_ids:
            road_nodes.add(node_id)
            uf.add(node_id)
        for a, b in zip(node_ids, node_ids[1:]):
            uf.union(a, b)

    return SnapshotGraph(
        uf, visible, road_nodes, missing_refs, pending_refs, pending,
    )


# ---------------------------------------------------------------------------
# Pair evaluation
# ---------------------------------------------------------------------------


def _endpoint_status(graph: SnapshotGraph, node_id: int) -> str:
    """Classify one endpoint of a saved pair in a snapshot.

    * ``absent`` -- node not visible (never uploaded or tombstoned);
    * ``pending_only`` -- node visible but only referenced by pending ways,
      so it has no road edge and the pending way cannot be judged;
    * ``present`` -- node is a vertex of a complete highway way;
    * ``isolated`` -- visible, not referenced by any pending way and not on
      any complete road (a real lone node, not pending data).
    """
    if node_id not in graph.visible:
        return ENDPOINT_ABSENT
    if node_id in graph.road_nodes:
        return ENDPOINT_PRESENT
    if node_id in graph.pending_refs:
        return ENDPOINT_PENDING_ONLY
    return ENDPOINT_ISOLATED


def evaluate_pair(graph: SnapshotGraph, start_node_id: int, end_node_id: int) -> dict:
    """Evaluate one start/end pair against one snapshot graph.

    ``absent``/``pending_only`` endpoints mean the snapshot cannot judge the
    pair.  An ``isolated`` endpoint is a definite vertex of the road network
    graph with no road edge: it is judged normally (e.g. a pair from a
    previously connected road to a lone node is a genuine disconnection).
    """
    start_status = _endpoint_status(graph, start_node_id)
    end_status = _endpoint_status(graph, end_node_id)
    endpoints_ok = start_status not in (ENDPOINT_ABSENT, ENDPOINT_PENDING_ONLY) \
        and end_status not in (ENDPOINT_ABSENT, ENDPOINT_PENDING_ONLY)

    if not endpoints_ok:
        state = STATE_INDETERMINATE
    elif graph.components.connected(start_node_id, end_node_id):
        state = STATE_CONNECTED
    else:
        state = STATE_DISCONNECTED

    return {
        "state": state,
        "start_status": start_status,
        "end_status": end_status,
        "start_component_size": (
            graph.components.component_size(start_node_id)
            if start_status != ENDPOINT_ABSENT else None
        ),
        "end_component_size": (
            graph.components.component_size(end_node_id)
            if end_status != ENDPOINT_ABSENT else None
        ),
        "pending_node_ids": sorted(graph.missing_node_ids),
    }


def classify(old: dict, new: dict) -> str:
    """Classify a pair between the two snapshots.

    ``endpoint_missing`` is returned when either snapshot cannot judge the
    pair; the caller keeps such pairs out of the three verdict counts.
    """
    if old["state"] == STATE_INDETERMINATE or new["state"] == STATE_INDETERMINATE:
        return CATEGORY_ENDPOINT_MISSING
    if old["state"] == STATE_CONNECTED and new["state"] == STATE_DISCONNECTED:
        return CATEGORY_NEW_DISCONNECTION
    if old["state"] == STATE_DISCONNECTED and new["state"] == STATE_CONNECTED:
        return CATEGORY_RECOVERED
    return CATEGORY_UNCHANGED


# ---------------------------------------------------------------------------
# Rule revisions
# ---------------------------------------------------------------------------


def current_rule_version(conn):
    """Newest registered rule revision (largest id; revisions are append-only)."""
    return conn.execute(
        "SELECT id, version FROM rule_versions ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _rule_version_row(conn, version: str):
    return conn.execute(
        "SELECT id, version FROM rule_versions WHERE version = ?", (version,)
    ).fetchone()


def _mark_other_reports_stale(conn, newest_id: int, newest_version: str) -> None:
    rows = conn.execute(
        "SELECT r.id, rv.version AS rule_version FROM connectivity_reports r "
        "JOIN rule_versions rv ON rv.id = r.rule_version_id "
        "WHERE r.rule_version_id <> ? AND r.stale = 0",
        (newest_id,),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE connectivity_reports SET stale = 1, stale_reason = ? WHERE id = ?",
            (
                f"规则已修订为 {newest_version}，本报告基于旧规则 "
                f"{row['rule_version']}，仅供留档参考，不能作为当前结论。",
                row["id"],
            ),
        )


def register_rule_version(conn, version: str) -> int:
    """Register ``version`` as a rule revision and return its id.

    Revisions are append-only: a known label keeps its original id.  A
    brand-new label becomes the newest revision and, in the same
    transaction, marks every report produced under another revision stale --
    kept for audit, never shown as the current conclusion.  ``init_db``
    registers the revision implemented by the running code at startup.
    """
    version = (version or "").strip()
    if not version or len(version) > 64:
        raise ConnectivityError("规则版本标识必须为 1–64 个字符")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO rule_versions (version) VALUES (?)", (version,)
        )
        row = _rule_version_row(conn, version)
        newest = current_rule_version(conn)
        if newest["id"] == row["id"]:
            _mark_other_reports_stale(conn, row["id"], row["version"])
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    return row["id"]


# ---------------------------------------------------------------------------
# Evaluation and report persistence
# ---------------------------------------------------------------------------


def committed_batch(conn, batch_id: int):
    row = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    if row is None:
        raise ConnectivityError(f"批次 #{batch_id} 不存在")
    if row["status"] != "committed":
        raise ConnectivityError(
            f"批次 #{batch_id} 状态为「{row['status']}」，只有已提交批次可参与质检"
        )
    return row


def _validate_batches(conn, from_batch_id: int, to_batch_id: int) -> None:
    if from_batch_id == to_batch_id:
        raise ConnectivityError("请选择两个不同的已提交批次")
    if from_batch_id > to_batch_id:
        # Direction matters: reversing swaps "new disconnection" with
        # "recovered", so refuse rather than silently reorder.
        raise ConnectivityError("起始批次必须早于结束批次（批次号较小者在前）")
    committed_batch(conn, from_batch_id)
    committed_batch(conn, to_batch_id)


def evaluate_pairs(
    conn, pair_rows, from_batch_id: int, to_batch_id: int
) -> tuple[list[dict], dict]:
    """Evaluate saved pairs between two committed snapshots (read-only)."""
    _validate_batches(conn, from_batch_id, to_batch_id)
    if not pair_rows:
        raise ConnectivityError("尚未保存任何起终点 node 组，无法生成质检报告")

    old_graph = snapshot_graph(conn, from_batch_id)
    new_graph = snapshot_graph(conn, to_batch_id)

    items: list[dict] = []
    summary = {
        "pair_count": 0,
        CATEGORY_NEW_DISCONNECTION: 0,
        CATEGORY_RECOVERED: 0,
        CATEGORY_UNCHANGED: 0,
        "endpoint_missing_pairs": 0,
        "pending_ways_old": [w.way_id for w in old_graph.pending_ways],
        "pending_ways_new": [w.way_id for w in new_graph.pending_ways],
        "missing_node_ids_old": sorted(old_graph.missing_node_ids),
        "missing_node_ids_new": sorted(new_graph.missing_node_ids),
    }
    for pair in pair_rows:
        start = int(pair["start_node_id"])
        end = int(pair["end_node_id"])
        old = evaluate_pair(old_graph, start, end)
        new = evaluate_pair(new_graph, start, end)
        category = classify(old, new)
        items.append({
            "pair_id": pair["id"],
            "label": pair["label"],
            "start_node_id": start,
            "end_node_id": end,
            "old": old,
            "new": new,
            "category": category,
        })
        summary["pair_count"] += 1
        if category == CATEGORY_ENDPOINT_MISSING:
            summary["endpoint_missing_pairs"] += 1
        else:
            summary[category] += 1
    return items, summary


def create_report(conn, from_batch_id: int, to_batch_id: int) -> int:
    """Compute and persist a report from the currently saved pairs.

    Pair read, graph evaluation and insert happen inside one
    ``BEGIN IMMEDIATE`` transaction.  The report is stamped with the rule
    version the running code actually implements
    (``config.CONNECTIVITY_RULE_VERSION``).  If a newer revision has been
    registered -- before or during the analysis -- the report is stored as
    stale: results produced by old rules must never become the current
    conclusion, only an archived record.
    """
    # Validate inputs before taking the write lock: an invalid selection must
    # not hold up concurrent uploads/revisions while it is rejected.
    _validate_batches(conn, from_batch_id, to_batch_id)
    conn.execute("BEGIN IMMEDIATE")
    try:
        running = _rule_version_row(conn, config.CONNECTIVITY_RULE_VERSION)
        if running is None:
            # init_db registers it at startup; register defensively so a
            # report always carries a real revision id.
            conn.execute(
                "INSERT OR IGNORE INTO rule_versions (version) VALUES (?)",
                (config.CONNECTIVITY_RULE_VERSION,),
            )
            running = _rule_version_row(conn, config.CONNECTIVITY_RULE_VERSION)

        pairs = conn.execute(
            "SELECT id, start_node_id, end_node_id, label "
            "FROM connectivity_pairs ORDER BY id"
        ).fetchall()
        items, summary = evaluate_pairs(conn, pairs, from_batch_id, to_batch_id)

        cur = conn.execute(
            "INSERT INTO connectivity_reports "
            "(from_batch_id, to_batch_id, rule_version_id, stale, stale_reason, "
            " from_pending_json, to_pending_json) "
            "VALUES (?, ?, ?, 0, NULL, ?, ?)",
            (
                from_batch_id,
                to_batch_id,
                running["id"],
                json.dumps({
                    "pending_way_ids": summary["pending_ways_old"],
                    "missing_node_ids": summary["missing_node_ids_old"],
                }),
                json.dumps({
                    "pending_way_ids": summary["pending_ways_new"],
                    "missing_node_ids": summary["missing_node_ids_new"],
                }),
            ),
        )
        report_id = cur.lastrowid
        for item in items:
            old, new = item["old"], item["new"]
            conn.execute(
                "INSERT INTO connectivity_report_items "
                "(report_id, pair_id, start_node_id, end_node_id, label, "
                " old_state, new_state, category, old_missing_json, new_missing_json, "
                " old_endpoint_json, new_endpoint_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    report_id,
                    item["pair_id"],
                    item["start_node_id"],
                    item["end_node_id"],
                    item["label"],
                    old["state"],
                    new["state"],
                    item["category"],
                    json.dumps([n for n, s in (
                        (item["start_node_id"], old["start_status"]),
                        (item["end_node_id"], old["end_status"]),
                    ) if s != ENDPOINT_PRESENT]),
                    json.dumps([n for n, s in (
                        (item["start_node_id"], new["start_status"]),
                        (item["end_node_id"], new["end_status"]),
                    ) if s != ENDPOINT_PRESENT]),
                    json.dumps({
                        "start": old["start_status"], "end": old["end_status"],
                    }),
                    json.dumps({
                        "start": new["start_status"], "end": new["end_status"],
                    }),
                ),
            )

        latest = current_rule_version(conn)
        if latest["id"] != running["id"]:
            # A newer revision is already registered (or became visible during
            # this analysis): this report was produced by older rules and
            # therefore cannot be the current conclusion.
            conn.execute(
                "UPDATE connectivity_reports SET stale = 1, stale_reason = ? "
                "WHERE id = ?",
                (
                    f"当前登记的规则版本为 {latest['version']}，本报告由旧规则 "
                    f"{running['version']} 生成，仅供留档参考，不能作为当前结论。",
                    report_id,
                ),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise
    return report_id


def load_report(conn, report_id: int):
    """Return ``(report_row, items, summary)`` or ``(None, None, None)``."""
    row = conn.execute(
        """
        SELECT r.*, rv.version AS rule_version,
               b1.filename AS from_filename, b1.status AS from_status,
               b2.filename AS to_filename, b2.status AS to_status
        FROM connectivity_reports r
        JOIN rule_versions rv ON rv.id = r.rule_version_id
        JOIN batches b1 ON b1.id = r.from_batch_id
        JOIN batches b2 ON b2.id = r.to_batch_id
        WHERE r.id = ?
        """,
        (report_id,),
    ).fetchone()
    if row is None:
        return None, None, None

    item_rows = conn.execute(
        "SELECT * FROM connectivity_report_items WHERE report_id = ? ORDER BY id",
        (report_id,),
    ).fetchall()
    items = []
    summary = {
        "pair_count": 0,
        CATEGORY_NEW_DISCONNECTION: 0,
        CATEGORY_RECOVERED: 0,
        CATEGORY_UNCHANGED: 0,
        "endpoint_missing_pairs": 0,
        "from_pending": _pending_snapshot(row["from_pending_json"]),
        "to_pending": _pending_snapshot(row["to_pending_json"]),
    }
    for ir in item_rows:
        item = dict(ir)
        item["old_missing"] = _refs_from_json(ir["old_missing_json"])
        item["new_missing"] = _refs_from_json(ir["new_missing_json"])
        item["old_endpoints"] = json.loads(ir["old_endpoint_json"])
        item["new_endpoints"] = json.loads(ir["new_endpoint_json"])
        items.append(item)
        summary["pair_count"] += 1
        if ir["category"] == CATEGORY_ENDPOINT_MISSING:
            summary["endpoint_missing_pairs"] += 1
        else:
            summary[ir["category"]] = summary.get(ir["category"], 0) + 1
    return row, items, summary


def list_reports(conn):
    return conn.execute(
        "SELECT r.*, rv.version AS rule_version, "
        "b1.filename AS from_filename, b2.filename AS to_filename "
        "FROM connectivity_reports r "
        "JOIN rule_versions rv ON rv.id = r.rule_version_id "
        "JOIN batches b1 ON b1.id = r.from_batch_id "
        "JOIN batches b2 ON b2.id = r.to_batch_id "
        "ORDER BY r.id DESC"
    ).fetchall()


# ---------------------------------------------------------------------------
# Saved start/end pairs
# ---------------------------------------------------------------------------


def list_pairs(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM connectivity_pairs ORDER BY id"
    ).fetchall()]


def add_pair(conn, start_node_id: int, end_node_id: int, label: str | None = None) -> None:
    """Save one start/end pair, enforcing the 20-pair cap atomically."""
    try:
        start_node_id = int(start_node_id)
        end_node_id = int(end_node_id)
    except (TypeError, ValueError) as exc:
        raise ConnectivityError("起终点必须是整数 node id") from exc
    if start_node_id < 1 or end_node_id < 1:
        raise ConnectivityError("node id 必须是正整数")
    if start_node_id == end_node_id:
        raise ConnectivityError("起点和终点不能是同一个 node")
    clean_label = (label or "").strip() or None
    if clean_label is not None and len(clean_label) > 80:
        raise ConnectivityError("备注最多 80 个字符")
    conn.execute("BEGIN IMMEDIATE")
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM connectivity_pairs"
        ).fetchone()["n"]
        if n >= config.MAX_WATCH_PAIRS:
            raise ConnectivityError(
                f"最多保存 {config.MAX_WATCH_PAIRS} 组起终点 node，请先删除已有组"
            )
        if conn.execute(
            "SELECT 1 FROM connectivity_pairs "
            "WHERE start_node_id = ? AND end_node_id = ?",
            (start_node_id, end_node_id),
        ).fetchone() is not None:
            raise ConnectivityError("这组起终点已经保存过了")
        conn.execute(
            "INSERT INTO connectivity_pairs (start_node_id, end_node_id, label) "
            "VALUES (?, ?, ?)",
            (start_node_id, end_node_id, clean_label),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.rollback()
        raise


def delete_pair(conn, pair_id: int) -> None:
    conn.execute("DELETE FROM connectivity_pairs WHERE id = ?", (pair_id,))
