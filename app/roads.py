"""Reconstruct the road layer as it was when any given batch was complete.

Because ``object_versions`` is append-only, the state *as of* batch B is
obtained by taking, for every object, the highest version whose
``batch_id <= B``.  Nothing that arrived in later batches can leak into an
earlier snapshot:

* a shared node moving in batch 5 changes every affected way's geometry in
  batch 5's export even when the way itself is never re-uploaded;
* the same way's geometry in the batch 3 export is still built from the
  node coordinates stored up to batch 3;
* a deleted referenced node (or one never yet uploaded) makes the whole way
  pending until a later batch supplies a visible version;
* once a way's references change, its old node ids no longer influence it.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

HIGHWAY_TAG = "highway"


@dataclass
class RoadFeature:
    way_id: int
    version: int
    batch_id: int
    highway: str
    name: str | None
    coordinates: list[list[float]]
    tags: dict[str, str]


@dataclass
class PendingWay:
    way_id: int
    version: int
    batch_id: int
    highway: str
    missing_node_ids: list[int]


def _current_objects(conn: sqlite3.Connection, batch_id: int) -> dict[tuple, sqlite3.Row]:
    """Return each object's highest-version state visible up to ``batch_id``."""
    rows = conn.execute(
        """
        SELECT o.*
        FROM object_versions o
        JOIN (
            SELECT object_type, object_id, MAX(version) AS max_version
            FROM object_versions
            WHERE batch_id <= ?
            GROUP BY object_type, object_id
        ) m
          ON o.object_type = m.object_type
         AND o.object_id = m.object_id
         AND o.version = m.max_version
        WHERE o.batch_id <= ?
        """,
        (batch_id, batch_id),
    ).fetchall()
    return {(row["object_type"], row["object_id"]): row for row in rows}


def build_road_layer(conn: sqlite3.Connection, batch_id: int):
    """Build (roads, pending_ways) for the committed batch ``batch_id``."""
    objects = _current_objects(conn, batch_id)
    roads: list[RoadFeature] = []
    pending: list[PendingWay] = []

    for (obj_type, obj_id), row in sorted(objects.items()):
        if obj_type != "way" or not row["visible"]:
            continue
        tags = json.loads(row["tags_json"])
        if HIGHWAY_TAG not in tags:
            continue  # only highway ways form the road layer

        refs = json.loads(row["refs_json"]) if row["refs_json"] is not None else []
        coordinates: list[list[float]] = []
        missing: list[int] = []
        for node_id in refs:
            node = objects.get(("node", node_id))
            if node is None or not node["visible"]:
                missing.append(node_id)
            else:
                coordinates.append([node["lon"], node["lat"]])

        if missing or len(coordinates) < 2:
            # Whole way waits until every referenced node is present.
            if not missing and len(coordinates) < 2:
                missing = list(refs)
            pending.append(PendingWay(
                way_id=obj_id,
                version=row["version"],
                batch_id=row["batch_id"],
                highway=tags[HIGHWAY_TAG],
                missing_node_ids=missing,
            ))
        else:
            roads.append(RoadFeature(
                way_id=obj_id,
                version=row["version"],
                batch_id=row["batch_id"],
                highway=tags[HIGHWAY_TAG],
                name=tags.get("name"),
                coordinates=coordinates,
                tags=tags,
            ))

    return roads, pending


def road_geojson(roads: list[RoadFeature]) -> dict:
    """Serialize roads as a GeoJSON FeatureCollection (lon, lat order)."""
    features = []
    for road in roads:
        props = dict(road.tags)
        props.setdefault("osm_way_id", road.way_id)
        props.setdefault("osm_version", road.version)
        props.setdefault("source_batch_id", road.batch_id)
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": road.coordinates},
            "properties": props,
        })
    return {"type": "FeatureCollection", "name": "highways", "features": features}


def batch_summary(conn: sqlite3.Connection, batch_id: int) -> dict:
    """Object counts visible as of a batch, for the detail page."""
    rows = conn.execute(
        """
        SELECT object_type,
               SUM(CASE WHEN visible = 1 THEN 1 ELSE 0 END) AS visible_count,
               SUM(CASE WHEN visible = 0 THEN 1 ELSE 0 END) AS deleted_count
        FROM object_versions
        WHERE (object_type, object_id, version) IN (
            SELECT object_type, object_id, MAX(version)
            FROM object_versions
            WHERE batch_id <= ?
            GROUP BY object_type, object_id
        )
        GROUP BY object_type
        """,
        (batch_id,),
    ).fetchall()
    summary = {"node_visible": 0, "node_deleted": 0, "way_visible": 0, "way_deleted": 0}
    for row in rows:
        prefix = "node" if row["object_type"] == "node" else "way"
        summary[f"{prefix}_visible"] = row["visible_count"]
        summary[f"{prefix}_deleted"] = row["deleted_count"]
    return summary
