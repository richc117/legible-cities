"""Render a schematic line graph to SVG.

LOOM ships ``transitmap``, which draws a perfectly good map -- but the animation
needs geometry it can address: one ``<path>`` per (line, edge) with a stable id,
so a train dot can be placed with ``path.getPointAtLength()``. So we draw it
ourselves from the post-``octi`` graph, reusing LOOM's solved line ordering via
``offsets.py`` and its labels via ``labels.py``.

Reproject the graph before rendering (see ``crs.to_mercator``) -- LOOM emits
lon/lat, where its 45-degree edges are not 45 degrees any more.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field

from .labels import Placement, Quad, Station, place, polyline_quads
from .names import display_name
from .linegraph import Coord, LineGraph
from .offsets import (cumulative_lengths, dedupe, offset_polyline, point_at,
                      track_offset)

@dataclass
class Style:
    line_width: float = 7.0
    line_gap: float = 1.6           # parallel track pitch, as a multiple of width
    station_radius: float = 4.2
    interchange_radius: float = 6.0
    station_stroke: float = 2.2
    label_size: float = 11.0
    label_offset: float = 9.0
    # Rough advance width per character as a fraction of font size.
    label_char_width: float = 0.56
    background: str = "#ffffff"
    station_fill: str = "#ffffff"
    station_stroke_color: str = "#111111"
    label_color: str = "#111111"
    default_line_color: str = "#888888"
    padding: float = 24.0

    # Emit the page furniture -- background, station markers, labels -- as CSS
    # custom properties so an embedding page can theme them, keeping the literal
    # values as fallbacks so a standalone SVG is unchanged.
    #
    # Line strokes are deliberately never themed. They are the agencies' own
    # colours, and the alternative an embedding page reaches for is a CSS
    # invert filter, which turns LA's A Line from #0072bc into orange.
    themed: bool = False

    @property
    def spacing(self) -> float:
        return self.line_width * self.line_gap

    def var(self, name: str, literal: str) -> str:
        """``var(--map-x, #fff)`` when themed, otherwise just the literal."""
        return f"var(--map-{name}, {literal})" if self.themed else literal


@dataclass
class Projection:
    """Maps graph coordinates to SVG pixel coordinates (y flipped)."""

    scale: float
    min_x: float
    max_y: float

    @classmethod
    def fit(cls, graph: LineGraph, width: float) -> Projection:
        min_x, min_y, max_x, max_y = graph.bounds()
        span_x = max(max_x - min_x, 1e-9)
        # Uniform scale in both axes: octi output is schematic, and squashing one
        # axis would break the 45-degree angles it worked to produce.
        return cls(width / span_x, min_x, max_y)

    def __call__(self, c: Coord) -> Coord:
        return ((c[0] - self.min_x) * self.scale, (self.max_y - c[1]) * self.scale)


def _path_d(points: list[Coord]) -> str:
    pts = dedupe(points)
    return (f"M {pts[0][0]:.2f} {pts[0][1]:.2f}"
            + "".join(f" L {x:.2f} {y:.2f}" for x, y in pts[1:]))


def _color(hexish: str | None, fallback: str) -> str:
    if not hexish:
        return fallback
    return hexish if hexish.startswith("#") else f"#{hexish}"


def _safe(token: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in token)


@dataclass
class TrackPath:
    """One line's drawn geometry across one edge -- the animation's atom."""

    element_id: str
    label: str
    src: str
    dst: str
    points: list[Coord]

    @property
    def length(self) -> float:
        return cumulative_lengths(self.points)[-1]


@dataclass
class RenderResult:
    svg: str
    width: float
    height: float
    projection: Projection
    tracks: dict[tuple[str, str, str], TrackPath] = field(default_factory=dict)
    node_xy: dict[str, Coord] = field(default_factory=dict)
    dropped_labels: list[str] = field(default_factory=list)

    def track(self, label: str, src: str, dst: str) -> TrackPath | None:
        """Look up a track path in either direction."""
        return self.tracks.get((label, src, dst)) or self.tracks.get((label, dst, src))


def build_tracks(graph: LineGraph, proj: Projection,
                 style: Style) -> dict[tuple[str, str, str], TrackPath]:
    tracks: dict[tuple[str, str, str], TrackPath] = {}
    for ei, edge in enumerate(graph.edges):
        centre = [proj(c) for c in edge.geometry]
        n = len(edge.lines)
        for i, line in enumerate(edge.lines):
            pts = offset_polyline(centre, track_offset(i, n, style.spacing))
            tracks[(line.label, edge.src, edge.dst)] = TrackPath(
                element_id=f"t{ei}_{_safe(line.label)}",
                label=line.label, src=edge.src, dst=edge.dst, points=pts)
    return tracks


def _horizontal_run(graph: LineGraph, node_id: str, proj: Projection) -> bool:
    """True when the lines through this node run roughly east-west.

    That is the case where an east-pinned label lands on top of its neighbours',
    so the placer should reach for the rotated candidates first.
    """
    here = proj(graph.nodes[node_id].coord)
    for e in graph.edges:
        if node_id not in (e.src, e.dst):
            continue
        far = proj(graph.nodes[e.dst if e.src == node_id else e.src].coord)
        dx, dy = far[0] - here[0], far[1] - here[1]
        if math.hypot(dx, dy) < 1e-6:
            continue
        if abs(math.degrees(math.atan2(dy, dx))) % 180 < 30 or abs(math.degrees(math.atan2(dy, dx))) % 180 > 150:
            return True
    return False


def render(graph: LineGraph, *, width: float = 1800.0, style: Style | None = None,
           labels: bool = True, title: str | None = None,
           line_order: list[str] | None = None) -> RenderResult:
    """Draw the graph. ``width`` sizes the network; the canvas grows for labels."""
    style = style or Style()
    proj = Projection.fit(graph, width)
    tracks = build_tracks(graph, proj, style)
    node_xy = {nid: proj(n.coord) for nid, n in graph.nodes.items()}

    colors: dict[str, str] = {}
    routes_at: dict[str, set[str]] = {}
    for e in graph.edges:
        for ln in e.lines:
            colors.setdefault(ln.label, _color(ln.color, style.default_line_color))
        for end in (e.src, e.dst):
            routes_at.setdefault(end, set()).update(ln.label for ln in e.lines)

    order = line_order or sorted(colors)

    # --- labels ---------------------------------------------------------
    placements: list[Placement] = []
    dropped: list[str] = []
    if labels:
        obstacles: list[Quad] = []
        for tp in tracks.values():
            obstacles += polyline_quads(tp.points, style.line_width)
        # How wide the track bundle reaches at each station, so its label can
        # clear it. An interchange where five lines run together needs a lot
        # more room than a single-track outer stop.
        bundle: dict[str, int] = {}
        for e in graph.edges:
            for end in (e.src, e.dst):
                bundle[end] = max(bundle.get(end, 0), len(e.lines))

        stations = []
        for node in graph.stations:
            if not node.station_label:
                continue
            x, y = node_xy[node.id]
            n = bundle.get(node.id, 1)
            stations.append(Station(
                text=display_name(node.station_label), x=x, y=y, key=node.id,
                importance=len(routes_at.get(node.id, ())),
                on_horizontal_run=_horizontal_run(graph, node.id, proj),
                clearance=(n - 1) / 2 * style.spacing + style.line_width / 2,
            ))
        placements, dropped_stations = place(
            stations, obstacles, size=style.label_size,
            char_width=style.label_char_width, offset=style.label_offset,
            marker_radius=style.interchange_radius)
        dropped = [s.text for s in dropped_stations]

    # --- canvas: derive from what is actually drawn ----------------------
    # Two boxes: the network alone, and the network plus its labels. Labels
    # reach well outside the track geometry, so a viewer that hides them is
    # otherwise left staring at a mostly empty canvas.
    net_xs: list[float] = []
    net_ys: list[float] = []
    for tp in tracks.values():
        for x, y in tp.points:
            net_xs += [x - style.line_width, x + style.line_width]
            net_ys += [y - style.line_width, y + style.line_width]
    for x, y in node_xy.values():
        net_xs += [x - style.interchange_radius, x + style.interchange_radius]
        net_ys += [y - style.interchange_radius, y + style.interchange_radius]

    xs, ys = list(net_xs), list(net_ys)
    for p in placements:
        xs += [p.quad.aabb[0], p.quad.aabb[2]]
        ys += [p.quad.aabb[1], p.quad.aabb[3]]

    pad = style.padding

    def box(bx: list[float], by: list[float]) -> tuple[float, float, float, float]:
        x0, x1 = min(bx) - pad, max(bx) + pad
        y0, y1 = min(by) - pad, max(by) + pad
        return x0, y0, x1 - x0, y1 - y0

    min_x, min_y, w, h = box(xs, ys)
    net_box = box(net_xs, net_ys)
    max_x, max_y = min_x + w, min_y + h

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
        f'viewBox="{min_x:.2f} {min_y:.2f} {w:.2f} {h:.2f}" '
        # Consumed by the animation's label toggle, inert in a standalone SVG.
        f'data-viewbox-nolabels="{net_box[0]:.2f} {net_box[1]:.2f} '
        f'{net_box[2]:.2f} {net_box[3]:.2f}" '
        f'font-family="Helvetica Neue, Helvetica, Arial, sans-serif">',
    ]
    if title:
        out.append(f"<title>{html.escape(title)}</title>")
    # Identified so a consumer that changes the viewBox can grow it to match.
    out.append(f'<rect id="backdrop" x="{min_x:.2f}" y="{min_y:.2f}" '
               f'width="{w:.2f}" height="{h:.2f}" '
               f'fill="{style.var("bg", style.background)}"/>')

    out.append('<g id="lines" fill="none" stroke-linecap="round" stroke-linejoin="round">')
    for label in order:
        if label not in colors:
            continue
        out.append(f'<g class="line" data-line="{html.escape(label)}" '
                   f'stroke="{colors[label]}" stroke-width="{style.line_width:.2f}">')
        for tp in tracks.values():
            if tp.label == label:
                # The endpoints let a consumer re-aim this segment at a
                # different layout; inert when the SVG is read on its own.
                out.append(f'<path id="{tp.element_id}" '
                           f'data-src="{html.escape(tp.src)}" '
                           f'data-dst="{html.escape(tp.dst)}" '
                           f'd="{_path_d(tp.points)}"/>')
        out.append("</g>")
    out.append("</g>")

    out.append('<g id="stations">')
    for node in graph.stations:
        x, y = node_xy[node.id]
        r = style.interchange_radius if len(routes_at.get(node.id, ())) > 1 else style.station_radius
        out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}" '
                   f'fill="{style.var("station-fill", style.station_fill)}" '
                   f'stroke="{style.var("station-stroke", style.station_stroke_color)}" '
                   f'stroke-width="{style.station_stroke:.2f}" '
                   f'data-node="{html.escape(node.id)}" '
                   f'data-station-id="{html.escape(node.station_id or "")}"/>')
    out.append("</g>")

    if placements:
        out.append(f'<g id="labels" font-size="{style.label_size:.1f}" '
                   f'fill="{style.var("label", style.label_color)}">')
        for p in placements:
            transform = (f' transform="rotate({p.rotate:.0f} {p.x:.2f} {p.y:.2f})"'
                         if p.rotate else "")
            # A haloed label sits over a line; the stroke is painted behind the
            # glyphs so the name stays legible against the colour.
            halo = (f' stroke="{style.var("bg", style.background)}" stroke-width="3.2"'
                    f' paint-order="stroke" stroke-linejoin="round"' if p.haloed else "")
            out.append(f'<text x="{p.x:.2f}" y="{p.y:.2f}" text-anchor="{p.anchor}"'
                       f' data-node="{html.escape(p.key)}"'
                       f'{transform}{halo}>{html.escape(p.text)}</text>')
        out.append("</g>")

    out.append('<g id="trains"></g>')
    out.append("</svg>")

    return RenderResult(svg="\n".join(out), width=w, height=h, projection=proj,
                        tracks=tracks, node_xy=node_xy, dropped_labels=dropped)


def octilinearity(graph: LineGraph, tol_deg: float = 1.0,
                  min_length: float = 0.0) -> tuple[float, float]:
    """(length on a 45-degree multiple, total length) -- the octi sanity check.

    Weighted by length rather than counted per segment. LOOM writes lon/lat at
    six decimals, which leaves a scatter of metre-scale stubs around station
    nodes whose angles are pure rounding noise; counting segments lets those
    dominate, while they are invisible on the drawn map.

    Measure this on a projected graph -- see ``crs.to_mercator``.
    """
    ok = total = 0.0
    for e in graph.edges:
        pts = dedupe(list(e.geometry))
        for a, b in zip(pts, pts[1:]):
            length = math.dist(a, b)
            if length < min_length:
                continue
            total += length
            ang = math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 45.0
            if min(ang, 45.0 - ang) <= tol_deg:
                ok += length
    return ok, total


# --------------------------------------------------------------- geographic

def resample(points: list[Coord], count: int) -> list[Coord]:
    """Redistribute a polyline onto ``count`` points, evenly by arc length.

    A morph lerps vertex i to vertex i, so the two polylines must agree on how
    many vertices there are. They never do on their own: a geographic edge
    follows the real curve in dozens of points, and its octilinear counterpart
    is two or three straight segments.
    """
    pts = dedupe(points)
    if count < 2 or len(pts) < 2:
        return [pts[0]] * max(count, 1) if pts else []
    cum = cumulative_lengths(pts)
    total = cum[-1]
    if total <= 0:
        return [pts[0]] * count
    return [point_at(pts, total * i / (count - 1)) for i in range(count)]


def bbox(groups) -> tuple[float, float, float, float]:
    xs = [p[0] for g in groups for p in g]
    ys = [p[1] for g in groups for p in g]
    return min(xs), min(ys), max(xs), max(ys)
