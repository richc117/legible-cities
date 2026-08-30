"""Typed model of LOOM's GeoJSON line-graph interchange format.

Every LOOM tool speaks this format. A FeatureCollection where::

    Point      node  -> properties: id, station_id?, station_label?
    LineString edge  -> properties: from, to, lines[{id, label, color}]

Node ``id`` values are LOOM-internal (hex pointers) and are NOT stable across
tools -- only ``station_id`` / ``station_label`` survive as identity. The order
of an edge's ``lines`` array is meaningful after the ``loom`` stage: it is the
solved left-to-right ordering of parallel tracks along the edge's from->to
direction, which is what ``offsets.py`` consumes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

Coord = tuple[float, float]


@dataclass(frozen=True)
class Line:
    """A transit route as it appears on an edge."""

    id: str
    label: str
    color: str | None = None


@dataclass
class Node:
    id: str
    coord: Coord
    station_id: str | None = None
    station_label: str | None = None
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def is_station(self) -> bool:
        """Non-station nodes are geometry-only junctions LOOM inserts."""
        return self.station_id is not None or self.station_label is not None


@dataclass
class Edge:
    src: str
    dst: str
    geometry: list[Coord]
    lines: list[Line]
    props: dict[str, Any] = field(default_factory=dict)

    @property
    def line_labels(self) -> list[str]:
        return [ln.label for ln in self.lines]


@dataclass
class LineGraph:
    nodes: dict[str, Node]
    edges: list[Edge]

    # -- construction ----------------------------------------------------

    @classmethod
    def from_geojson(cls, obj: dict[str, Any] | str | Path) -> LineGraph:
        if isinstance(obj, (str, Path)):
            obj = json.loads(Path(obj).read_text())

        nodes: dict[str, Node] = {}
        edges: list[Edge] = []
        for feat in obj["features"]:
            geom, props = feat["geometry"], feat.get("properties", {})
            if geom["type"] == "Point":
                nid = str(props["id"])
                nodes[nid] = Node(
                    id=nid,
                    coord=tuple(geom["coordinates"][:2]),
                    station_id=props.get("station_id"),
                    station_label=props.get("station_label"),
                    props={k: v for k, v in props.items()
                           if k not in {"id", "station_id", "station_label"}},
                )
            elif geom["type"] == "LineString":
                edges.append(Edge(
                    src=str(props["from"]),
                    dst=str(props["to"]),
                    geometry=[tuple(c[:2]) for c in geom["coordinates"]],
                    lines=[Line(id=str(l["id"]), label=str(l["label"]), color=l.get("color"))
                           for l in props.get("lines", [])],
                    props={k: v for k, v in props.items() if k not in {"from", "to", "lines"}},
                ))
        return cls(nodes=nodes, edges=edges)

    def to_geojson(self) -> dict[str, Any]:
        feats: list[dict[str, Any]] = []
        for n in self.nodes.values():
            props = {"id": n.id, **n.props}
            if n.station_id is not None:
                props["station_id"] = n.station_id
            if n.station_label is not None:
                props["station_label"] = n.station_label
            feats.append({"type": "Feature",
                          "geometry": {"type": "Point", "coordinates": list(n.coord)},
                          "properties": props})
        for e in self.edges:
            feats.append({"type": "Feature",
                          "geometry": {"type": "LineString",
                                       "coordinates": [list(c) for c in e.geometry]},
                          "properties": {
                              "from": e.src, "to": e.dst,
                              "lines": [{"id": l.id, "label": l.label,
                                         **({"color": l.color} if l.color else {})}
                                        for l in e.lines],
                              **e.props}})
        return {"type": "FeatureCollection", "features": feats}

    # -- queries ---------------------------------------------------------

    @property
    def stations(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.is_station]

    @property
    def labels(self) -> list[str]:
        """Every distinct line label in the graph, sorted."""
        return sorted({ln.label for e in self.edges for ln in e.lines})

    def adjacency(self) -> dict[str, list[tuple[Edge, str]]]:
        """node id -> [(incident edge, node at the other end)]."""
        adj: dict[str, list[tuple[Edge, str]]] = defaultdict(list)
        for e in self.edges:
            adj[e.src].append((e, e.dst))
            adj[e.dst].append((e, e.src))
        return adj

    def edges_for(self, label: str) -> Iterator[Edge]:
        for e in self.edges:
            if any(ln.label == label for ln in e.lines):
                yield e

    def reproject(self, fn) -> LineGraph:
        """Return a copy with every coordinate passed through ``fn``."""
        return LineGraph(
            nodes={nid: Node(id=n.id, coord=fn(n.coord), station_id=n.station_id,
                             station_label=n.station_label, props=dict(n.props))
                   for nid, n in self.nodes.items()},
            edges=[Edge(src=e.src, dst=e.dst, geometry=[fn(c) for c in e.geometry],
                        lines=list(e.lines), props=dict(e.props))
                   for e in self.edges],
        )

    def bounds(self) -> tuple[float, float, float, float]:
        xs = [c[0] for e in self.edges for c in e.geometry] + [n.coord[0] for n in self.nodes.values()]
        ys = [c[1] for e in self.edges for c in e.geometry] + [n.coord[1] for n in self.nodes.values()]
        return min(xs), min(ys), max(xs), max(ys)

    def summary(self) -> str:
        st = self.stations
        return (f"{len(self.nodes)} nodes ({len(st)} stations, {len(self.nodes) - len(st)} junctions), "
                f"{len(self.edges)} edges, lines: {', '.join(self.labels)}")
