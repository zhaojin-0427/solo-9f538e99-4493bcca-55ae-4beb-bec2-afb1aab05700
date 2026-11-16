"""Batch ingestion and version-merge rules.

Merge semantics for every parsed element, keyed by (object type, id, version):

* a version lower than the highest already stored version is an *old*
  version and is silently ignored -- it can never resurrect or modify an
  object, including deleted ones ("旧版重传不能恢复对象");
* a version already stored with identical content is a duplicate and is
  skipped (re-uploading the same file is idempotent);
* a version already stored with *different* content means the same (id,
  version) carries conflicting payloads -- the entire batch is rejected,
  leaving no partial state behind;
* a higher version replaces the object's current state.  A version with
  ``visible=false`` is a tombstone: the object stays in the version history
  but no longer exists on the map ("删除后仍保留版本").

All of this happens inside one ``BEGIN IMMEDIATE`` transaction together with
the batch's status flip, so each batch takes effect atomically.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from .osm_parser import ParsedElement, parse_osm, OSMParseError

COORD_DECIMALS = 7  # OSM stores at most 7 decimal places (~1 cm).


class BatchRejected(Exception):
    """Raised when a batch fails validation; the batch row is marked rejected."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class IngestResult:
    batch_id: int
    filename: str
    inserted_nodes: int = 0
    inserted_ways: int = 0
    ignored_old: int = 0
    duplicates: int = 0


def _norm_coord(value: float | None) -> float | None:
    return None if value is None else round(float(value), COORD_DECIMALS)


def canonical_payload(el: ParsedElement) -> tuple:
    """Canonical, comparable representation of an element's content."""
    tags_json = json.dumps(el.tags, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if el.object_type == "node":
        return (int(el.visible), _norm_coord(el.lat), _norm_coord(el.lon), tags_json, None)
    refs_json = json.dumps(el.refs, separators=(",", ":")) if el.visible else None
    return (int(el.visible), None, None, tags_json, refs_json)


def _row_payload(row: sqlite3.Row) -> tuple:
    return (
        int(row["visible"]),
        _norm_coord(row["lat"]),
        _norm_coord(row["lon"]),
        json.dumps(json.loads(row["tags_json"]), ensure_ascii=False,
                   sort_keys=True, separators=(",", ":")),
        row["refs_json"],
    )


def ingest_stream(conn: sqlite3.Connection, filename: str, stream) -> IngestResult:
    """Parse and atomically ingest one uploaded file.

    On failure the object-version transaction is rolled back and a rejected
    batch row (with the reason) remains for auditing.  ``BatchRejected`` /
    ``OSMParseError`` carry the human-readable reason.
    """
    # 1. Reserve the batch id in its own (auto-commit) statement first, so
    #    that even a parse/validation failure leaves an auditable rejected
    #    batch row while no object row can survive (the object transaction
    #    is only opened afterwards).
    cur = conn.execute(
        "INSERT INTO batches (filename, status) VALUES (?, 'processing')", (filename,)
    )
    batch_id = cur.lastrowid

    result = IngestResult(batch_id=batch_id, filename=filename)
    seen_in_file: dict[tuple[str, int, int], tuple] = {}

    def reject(reason: str) -> None:
        raise BatchRejected(reason)

    try:
        # Parse inside the guarded region: malformed input marks the batch
        # rejected instead of leaving an untracked failure.
        elements = parse_osm(stream)

        # One write transaction for every object row and the status flip.
        conn.execute("BEGIN IMMEDIATE")
        for el in elements:
            key = (el.object_type, el.object_id, el.version)
            payload = canonical_payload(el)

            # Duplicates inside the same uploaded file.
            if key in seen_in_file:
                if seen_in_file[key] != payload:
                    reject(
                        f"文件内同一对象 {el.object_type} {el.object_id} v{el.version} "
                        "出现两种不同内容，整批拒绝"
                    )
                result.duplicates += 1
                continue

            max_row = conn.execute(
                "SELECT MAX(version) AS v FROM object_versions "
                "WHERE object_type = ? AND object_id = ?",
                (el.object_type, el.object_id),
            ).fetchone()
            max_version = max_row["v"]
            if max_version is not None and el.version < max_version:
                # Old version first: ignored, and can never undo a newer
                # state (in particular it cannot resurrect a deletion).
                result.ignored_old += 1
                seen_in_file[key] = payload
                continue

            existing = conn.execute(
                "SELECT * FROM object_versions WHERE object_type = ? AND object_id = ? "
                "AND version = ?",
                key,
            ).fetchone()

            if existing is not None:
                # Same (id, version) already in the warehouse at the current
                # highest version: identical content is a duplicate, any
                # difference is a same-version conflict -> reject the batch.
                if _row_payload(existing) != payload:
                    reject(
                        f"同版异内容冲突: {el.object_type} {el.object_id} v{el.version} "
                        "已入仓但内容不同，整批拒绝"
                    )
                result.duplicates += 1
                seen_in_file[key] = payload
                continue

            conn.execute(
                "INSERT INTO object_versions (object_type, object_id, version, visible, "
                "lat, lon, tags_json, refs_json, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    el.object_type,
                    el.object_id,
                    el.version,
                    int(el.visible),
                    payload[1],
                    payload[2],
                    payload[3],
                    payload[4],
                    batch_id,
                ),
            )
            seen_in_file[key] = payload
            if el.object_type == "node":
                result.inserted_nodes += 1
            else:
                result.inserted_ways += 1

        conn.execute(
            "UPDATE batches SET status = 'committed', error = NULL WHERE id = ?",
            (batch_id,),
        )
        conn.execute("COMMIT")
    except (BatchRejected, OSMParseError) as exc:
        conn.rollback()
        conn.execute(
            "UPDATE batches SET status = 'rejected', error = ? WHERE id = ?",
            (str(exc), batch_id),
        )
        raise
    except Exception as exc:
        conn.rollback()
        conn.execute(
            "UPDATE batches SET status = 'rejected', error = ? WHERE id = ?",
            (f"入仓过程发生异常，事务已回滚: {exc!r}", batch_id),
        )
        raise

    return result
