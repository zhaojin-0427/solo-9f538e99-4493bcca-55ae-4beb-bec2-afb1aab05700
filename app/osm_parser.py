"""Parser for OSM data excerpts (``.osm``) and offline change files (``.osc``).

Only ``node`` and ``way`` objects are accepted; any ``relation`` (or other
unsupported object) makes the whole upload invalid, matching the requirement
that uploads contain exclusively nodes and ways.

The parser is deliberately strict about the fields ingestion depends on
(``id``, ``version``, node coordinates, way node references) but ignores
document-level elements such as ``bounds`` or ``note``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import BinaryIO

from defusedxml import ElementTree as ET

ALLOWED_TYPES = ("node", "way")
CHANGE_BLOCKS = ("create", "modify", "delete")


class OSMParseError(ValueError):
    """Raised when an uploaded file is not a valid node/way-only OSM document."""


@dataclass(frozen=True)
class ParsedElement:
    object_type: str           # 'node' | 'way'
    object_id: int
    version: int
    visible: bool
    lat: float | None = None
    lon: float | None = None
    tags: dict[str, str] = field(default_factory=dict)
    refs: list[int] = field(default_factory=list)
    change_op: str | None = None  # 'create' | 'modify' | 'delete' | None (.osm)


def _local_name(tag: str) -> str:
    """Strip an XML namespace, e.g. ``{...}node`` -> ``node``."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _require_int(elem, attr: str, kind: str, obj_id: str | None) -> int:
    raw = elem.get(attr)
    if raw is None or raw.strip() == "":
        raise OSMParseError(
            f"{kind} 元素缺少必需属性 {attr}"
            + (f" (id={obj_id})" if obj_id is not None else "")
        )
    try:
        value = int(raw)
    except ValueError as exc:
        raise OSMParseError(f"{kind} 的 {attr} 不是整数: {raw!r}") from exc
    return value


def _parse_node(elem, change_op: str | None) -> ParsedElement:
    obj_id = elem.get("id")
    oid = _require_int(elem, "id", "node", None)
    version = _require_int(elem, "version", "node", obj_id)
    if oid < 1 or version < 1:
        raise OSMParseError(f"node id/version 必须为正整数 (id={oid}, version={version})")

    visible_raw = elem.get("visible")
    visible = True if visible_raw is None else visible_raw.strip() != "false"
    if change_op == "delete":
        visible = False

    lat = lon = None
    if visible:
        lat_raw, lon_raw = elem.get("lat"), elem.get("lon")
        if lat_raw is None or lon_raw is None:
            raise OSMParseError(f"可见 node {oid} 缺少 lat/lon 坐标")
        try:
            lat, lon = float(lat_raw), float(lon_raw)
        except ValueError as exc:
            raise OSMParseError(f"node {oid} 的坐标不是数字: lat={lat_raw!r}, lon={lon_raw!r}") from exc
        if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
            raise OSMParseError(f"node {oid} 坐标超出经纬度范围: lat={lat}, lon={lon}")

    tags = _parse_tags(elem, "node", oid)
    return ParsedElement("node", oid, version, visible, lat, lon, tags, [], change_op)


def _parse_way(elem, change_op: str | None) -> ParsedElement:
    obj_id = elem.get("id")
    oid = _require_int(elem, "id", "way", None)
    version = _require_int(elem, "version", "way", obj_id)
    if oid < 1 or version < 1:
        raise OSMParseError(f"way id/version 必须为正整数 (id={oid}, version={version})")

    visible_raw = elem.get("visible")
    visible = True if visible_raw is None else visible_raw.strip() != "false"
    if change_op == "delete":
        visible = False

    tags: dict[str, str] = {}
    refs: list[int] = []
    for child in elem:
        name = _local_name(child.tag)
        if name == "tag":
            key = child.get("k")
            if key is None or key == "":
                raise OSMParseError(f"way {oid} 存在缺少 k 属性的 tag")
            if key in tags:
                raise OSMParseError(f"way {oid} 的 tag 键重复: {key!r}")
            tags[key] = child.get("v", "")
        elif name == "nd":
            ref = _require_int(child, "ref", "way 的 nd", str(oid))
            if ref < 1:
                raise OSMParseError(f"way {oid} 引用了非法 node id: {ref}")
            refs.append(ref)
        else:
            raise OSMParseError(f"way {oid} 内出现不支持的子元素 <{name}>，仅允许 tag/nd")

    return ParsedElement("way", oid, version, visible, None, None, tags, refs, change_op)


def _parse_tags(elem, kind: str, oid: int) -> dict[str, str]:
    tags: dict[str, str] = {}
    for child in elem:
        name = _local_name(child.tag)
        if name == "tag":
            key = child.get("k")
            if key is None or key == "":
                raise OSMParseError(f"{kind} {oid} 存在缺少 k 属性的 tag")
            if key in tags:
                raise OSMParseError(f"{kind} {oid} 的 tag 键重复: {key!r}")
            tags[key] = child.get("v", "")
        elif name != "nd":
            raise OSMParseError(f"{kind} {oid} 内出现不支持的子元素 <{name}>，仅允许 tag")
    return tags


def parse_osm(stream: BinaryIO) -> list[ParsedElement]:
    """Parse an uploaded ``.osm`` / ``.osc`` byte stream into element records.

    Order inside the document is irrelevant: the returned list is merged by
    (type, id, version), so out-of-order change files are handled the same
    way as ordered ones.
    """
    try:
        tree = ET.parse(stream)
    except ET.ParseError as exc:
        raise OSMParseError(f"XML 解析失败: {exc}") from exc

    root = tree.getroot()
    root_name = _local_name(root.tag)
    elements: list[ParsedElement] = []

    if root_name == "osm":
        for child in root:
            name = _local_name(child.tag)
            if name in ("node", "way"):
                elements.append(_parse_node(child, None) if name == "node"
                                else _parse_way(child, None))
            elif name in CHANGE_BLOCKS:
                raise OSMParseError("普通 .osm 文档中不应出现 create/modify/delete 变更块")
            elif name == "relation":
                raise OSMParseError("仅支持 node、way 对象，发现 relation，整批拒绝")
            # bounds/note/meta/... are document-level metadata and are ignored.
    elif root_name == "osmChange":
        for block in root:
            block_name = _local_name(block.tag)
            if block_name not in CHANGE_BLOCKS:
                raise OSMParseError(f"osmChange 内出现未知变更块 <{block_name}>")
            for child in block:
                name = _local_name(child.tag)
                if name == "node":
                    elements.append(_parse_node(child, block_name))
                elif name == "way":
                    elements.append(_parse_way(child, block_name))
                elif name == "relation":
                    raise OSMParseError("仅支持 node、way 对象，发现 relation，整批拒绝")
                else:
                    raise OSMParseError(
                        f"变更块 {block_name} 内出现不支持的元素 <{name}>，仅允许 node/way"
                    )
    else:
        raise OSMParseError(f"根元素 <{root_name}> 不是 osm 或 osmChange，无法识别文件格式")

    if not elements:
        raise OSMParseError("文件中没有任何 node/way 元素，无法入仓")
    return elements
