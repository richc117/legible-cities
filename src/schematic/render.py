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

import base64
import functools
import html
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .labels import (Measure, Placement, Quad, Station, advance_measure, em_measure, place,
                     polyline_quads)
from .names import display_name
from .linegraph import Coord, LineGraph, ordered_labels
from .offsets import cumulative_lengths, dedupe, offset_polyline, point_at, slot_offsets

@dataclass
class Style:
    # The defaults here are the engine's own look and no preset's: ``PRESETS``
    # below names three others, each a complete set of the eight numbers, and
    # none equals this (a test holds it). Changing a default here changes
    # every map drawn without a style; it is not how a look is added.
    line_width: float = 7.0
    line_gap: float = 1.6           # parallel track pitch, as a multiple of width
    station_radius: float = 4.2
    interchange_radius: float = 6.0
    station_stroke: float = 2.2
    label_size: float = 11.0
    label_offset: float = 9.0
    # Rough advance width per character as a fraction of font size: how the
    # system face is measured. A bundled face is measured from its own table.
    label_char_width: float = 0.56
    # The face the station names are drawn and measured in, one of
    # ``LABEL_FONTS``. "system" is the stack the viewer's own fonts answer,
    # and draws exactly what was drawn before there was a choice.
    label_font: str = "system"
    background: str = "#ffffff"
    station_fill: str = "#ffffff"
    station_stroke_color: str = "#111111"
    label_color: str = "#111111"
    default_line_color: str = "#888888"
    padding: float = 24.0
    # The marker of a station on one line, and of one where lines meet: one of
    # ``STATION_SHAPES`` and of ``INTERCHANGE_SHAPES`` (issue 74). Circles, the
    # default, draw exactly what was drawn before there was a choice.
    station_shape: str = "circle"
    interchange_shape: str = "circle"

    # Emit the page furniture -- background, station markers, labels -- as CSS
    # custom properties so an embedding page can theme them, keeping the literal
    # values as fallbacks so a standalone SVG is unchanged.
    #
    # Line strokes are deliberately never themed. They are the agencies' own
    # colours, and the alternative an embedding page reaches for is a CSS
    # invert filter, which turns LA's A Line from #0072bc into orange.
    themed: bool = False

    def __post_init__(self) -> None:
        # The marker fields are names, so a Python caller's slip would draw
        # something else in silence; refused here as the server refuses it.
        # An interchange is never a tick (``INTERCHANGE_SHAPES``).
        for name, shapes in STYLE_SHAPES.items():
            value = getattr(self, name)
            if not isinstance(value, str) or value not in shapes:
                raise ValueError(f"{name} must be {', '.join(shapes[:-1])} or {shapes[-1]}, "
                                 f"not {value!r}")

    @property
    def spacing(self) -> float:
        return self.line_width * self.line_gap

    def var(self, name: str, literal: str) -> str:
        """``var(--map-x, #fff)`` when themed, otherwise just the literal."""
        return f"var(--map-{name}, {literal})" if self.themed else literal


# What a client may ask of each numeric field of ``Style``: the closed range
# and the unit it is in. The one table the server's sentences (``serve._style``)
# and the protocol schema's bounds and descriptions agree with; a test holds
# them to it. Every unit but one is SVG user units at the map's width, since
# the map is fitted to ``width`` (1,800 by default), so ``line_width`` 7 is
# seven of 1,800. ``line_gap`` is a multiple of ``line_width`` and has no unit
# (None). ``Style`` itself enforces none of this: the thumbnail's scaled style
# and the site's are the engine's own and sit outside these ranges.
USER_UNITS = "SVG user units at the map's width"
STYLE_RANGES: dict[str, tuple[float, float, str | None]] = {
    "line_width": (1, 24, USER_UNITS),
    "line_gap": (1, 3, None),
    "station_radius": (1, 20, USER_UNITS),
    "interchange_radius": (1, 30, USER_UNITS),
    "station_stroke": (0, 8, USER_UNITS),
    "label_size": (6, 32, USER_UNITS),
    "label_offset": (0, 40, USER_UNITS),
    "padding": (0, 200, USER_UNITS),
}

# What a client may ask of the two marker fields of ``Style`` (issue 74), the
# one table the server's sentences and the schema's enums agree with. A
# circle is the ring every map drew before; a tick is TfL's (``TICK``); a
# square is turned with the line. An interchange is never a tick: both TfL and
# Beck pair ticks with rings, and a tick has no side where lines meet.
STATION_SHAPES = ("circle", "tick", "square")
INTERCHANGE_SHAPES = ("circle", "square")
STYLE_SHAPES: dict[str, tuple[str, ...]] = {
    "station_shape": STATION_SHAPES,
    "interchange_shape": INTERCHANGE_SHAPES,
}

# TfL's station tick, as a multiple of the line's thickness: its Line diagram
# standard (January 2025) has "Station ticks are 0.66x squared".
TICK = 0.66


# Named looks, each a name over the eight numbers of ``STYLE_RANGES`` (issue
# 73), and a marker of ``STYLE_SHAPES`` where the look has one of its own
# (issue 74): what ``style.presets`` answers and what ``map.build``'s
# ``{"preset": name}`` resolves to, in this order. The theme owns the colours,
# so a preset carries none, and nothing here is ``Style``'s default. Each
# value is inside its range with the interchange above the station, and no
# preset equals the default or another; a test holds all three.
#
# beck: TfL's ratios (its Line diagram standard, January 2025). An interchange
#   is a ring half a line thick round an interior two lines wide, so its outer
#   diameter is three lines; names a line and two-thirds off it; a third of a
#   line between parallel strokes, because feeds repeat trunk colours. A
#   station is TfL's tick and an interchange keeps the ring, as Beck drew them.
# blueprint: thin strokes, small round stations and generous ground.
# paper: print-like; a label of 12 is TfL's "x-height equals the line's
#   thickness" at Helvetica Neue's 0.517 em.
PRESETS: dict[str, dict[str, float | str]] = {
    "beck": {"line_width": 6, "line_gap": 1.33, "station_radius": 3.6,
             "interchange_radius": 7.5, "station_stroke": 3, "label_size": 11,
             "label_offset": 10, "padding": 24, "station_shape": "tick"},
    "blueprint": {"line_width": 4, "line_gap": 2, "station_radius": 3,
                  "interchange_radius": 4.5, "station_stroke": 1.5, "label_size": 10,
                  "label_offset": 8, "padding": 32},
    "paper": {"line_width": 6, "line_gap": 1.6, "station_radius": 3.6,
              "interchange_radius": 5.5, "station_stroke": 1.8, "label_size": 12,
              "label_offset": 10, "padding": 28},
}


# The faces a map's station names can be drawn in (issue 76), in the order a
# client is offered them. "system" is today's stack and the 0.56 em estimate;
# the other two ship with the package as Latin subsets under the SIL Open Font
# License (``fonts/<name>/``, made by ``bin/build-fonts``), each measured name
# by name from its own advances and embedded in the SVG only when chosen.
LABEL_FONTS = ("system", "inter", "atkinson-hyperlegible-next")
SYSTEM_FONTS = "Helvetica Neue, Helvetica, Arial, sans-serif"
FONTS_DIR = Path(__file__).parent / "fonts"


@dataclass(frozen=True)
class LabelFace:
    """A bundled face: its family name, the CSS that embeds it, and its
    advances in font units by code point."""

    name: str
    family: str
    font_face: str
    units_per_em: int
    average: float
    advances: dict[int, int] = field(repr=False)

    def measure(self) -> Measure:
        return advance_measure(self.advances, self.units_per_em, self.average)


@functools.lru_cache(maxsize=None)
def label_face(name: str) -> LabelFace | None:
    """The bundled face ``name``, read once from the package, or None for
    "system", which has no file. A ValueError for a name not in
    ``LABEL_FONTS``: the server refuses one in its own sentence first."""
    if name not in LABEL_FONTS:
        raise ValueError(f"label_font must be one of {', '.join(LABEL_FONTS)}, not {name!r}")
    if name == "system":
        return None
    folder = FONTS_DIR / name
    table = json.loads((folder / "advances.json").read_text(encoding="utf-8"))
    data = base64.b64encode((folder / "regular.woff2").read_bytes()).decode("ascii")
    return LabelFace(
        name=name, family=table["family"],
        font_face=(f'@font-face{{font-family:"{table["family"]}";'
                   f'src:url(data:font/woff2;base64,{data}) format("woff2")}}'),
        units_per_em=table["unitsPerEm"], average=table["average"],
        advances={int(cp): advance for cp, advance in table["advances"].items()})


# How one line is stroked (issue 55; the note at the top of ``offsets.py``):
# its width, a casing either side of it and a dash, which ``map.build`` takes
# per line in ``lines`` and ``serve._lines`` judges against these. ``width`` is
# a multiple of ``line_width``, the band of emphasis the issue settled on: much
# outside it a line's round caps no longer bridge the hop its neighbours make
# at a node. A casing widens its line's slot as well, so a wide one can open
# such a gap in a neighbour where its line leaves the bundle. The casing's
# ``width`` is on each side, in the same unit, and its colour is ``#rrggbb``.
LINE_WIDTH_RANGE = (0.75, 1.5)
CASING_WIDTH_RANGE = (0.0, 1.0)
LINE_DASHES = ("solid", "dashed", "dotted")


@dataclass(frozen=True)
class LineStroke:
    """One line's width, casing and dash, each a multiple of the style's
    ``line_width`` where it is a size. The default is the line every map
    drew before there was a choice, and a line at the default is left out of
    ``line_strokes``, so it is drawn as it always was."""

    width: float = 1.0
    casing: float = 0.0
    casing_color: str = ""
    dash: str = "solid"

    def __post_init__(self) -> None:
        # Judged here as the server judges them, so a Python caller's slip is
        # refused rather than drawn: a width outside the band breaks the
        # caps' bridge at a node, and a dash that is not one of the three
        # names would reach the page unread.
        low, high = LINE_WIDTH_RANGE
        if not low <= self.width <= high:
            raise ValueError(f"a line's width must be from {low:g} to {high:g}, "
                             f"not {self.width!r}")
        low, high = CASING_WIDTH_RANGE
        if not low <= self.casing <= high:
            raise ValueError(f"a line's casing width must be from {low:g} to {high:g}, "
                             f"not {self.casing!r}")
        if self.casing > 0:
            check_color(self.casing_color, "a line's casing colour")
        if self.dash not in LINE_DASHES:
            raise ValueError(f"a line's dash must be {', '.join(LINE_DASHES[:-1])} or "
                             f"{LINE_DASHES[-1]}, not {self.dash!r}")

    @property
    def slot(self) -> float:
        """The room the line takes across an edge, its casing included, as a
        multiple of ``line_width``: the note's ``w + 2c``."""
        return self.width + 2 * self.casing

    def dasharray(self, t: float) -> str | None:
        """The stroke's dash for a stroke ``t`` wide, allowing for the round
        caps, which add t/2 to each end of every dash: dashed ``t 2t`` shows
        2t on and t off, dotted ``0 1.6t`` dots of diameter t 1.6t apart, and
        solid writes nothing."""
        if self.dash == "dashed":
            return f"{t:.2f} {2 * t:.2f}"
        if self.dash == "dotted":
            return f"0 {1.6 * t:.2f}"
        return None


def line_strokes(lines: dict[str, dict] | None) -> dict[str, LineStroke]:
    """The strokes a client chose, by label, from ``map.build``'s ``lines``
    as ``serve._lines`` checked them: ``width``, ``casing`` (``{width,
    color}``) and ``dash``. A line that chose none of them, or only their
    defaults (width 1, a casing of width 0, solid), is left out, so a map
    whose lines are all left out is drawn exactly as it was before there was
    a choice. A label the map does not draw is kept; nothing reads it."""
    out: dict[str, LineStroke] = {}
    for label, chosen in (lines or {}).items():
        casing = chosen.get("casing") or {}
        stroke = LineStroke(width=chosen.get("width", 1.0),
                            casing=casing.get("width", 0.0),
                            casing_color=casing.get("color", "") if casing.get("width") else "",
                            dash=chosen.get("dash", "solid"))
        if stroke != LineStroke():
            out[label] = stroke
    return out


def label_measure(style: Style) -> Measure:
    """How wide ``style``'s labels are drawn: the chosen face's own
    advances, or the system face's estimate of ``label_char_width`` an em
    a character."""
    face = label_face(style.label_font)
    return face.measure() if face else em_measure(style.label_char_width)


def preset_style(name: str) -> dict[str, float | str]:
    """The fields of the preset ``name``, a copy a caller may change.
    A KeyError for a name not in ``PRESETS``: the server refuses an unknown
    name in its own sentence before it gets here."""
    return dict(PRESETS[name])


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


# A colour a caller chooses is written the one way, ``#rrggbb``: what the
# schema says, what the server checks, and what this module refuses
# otherwise. GTFS's own six digits without the hash are the feed's, not a
# caller's, and ``_color`` keeps taking them -- but it holds them to the same
# six digits, because a feed is an agency's file and reaches a ``stroke``
# attribute, where a quote would end the attribute and start markup.
HEX_COLOR_PATTERN = r"^#[0-9a-fA-F]{6}$"
_HEX_COLOR = re.compile(HEX_COLOR_PATTERN)


def _color(hexish: str | None, fallback: str) -> str:
    """The feed's own ``route_color`` as GTFS writes it -- six hex digits,
    with or without the hash -- else ``fallback``. Anything else is dropped
    rather than refused: one unusable row in ``routes.txt`` must not stop a
    city being drawn, and the caller has nothing to correct."""
    if not isinstance(hexish, str) or not hexish:
        return fallback
    value = hexish if hexish.startswith("#") else f"#{hexish}"
    return value if _HEX_COLOR.fullmatch(value) else fallback


def _attr(value: str) -> str:
    """A value on its way into an attribute. Colours go through here too:
    validated or not, nothing this module writes into markup is trusted to
    be free of a quote."""
    return html.escape(str(value), quote=True)


def check_color(value: object, what: str) -> str:
    """``value`` as a colour written ``#rrggbb``, or a ValueError naming
    ``what`` was wrong."""
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise ValueError(f"{what} must be a colour written #rrggbb, not {value!r}")
    return value


def line_colors(graph: LineGraph, *, default: str,
                overrides: dict[str, str] | None = None) -> dict[str, str]:
    """Every line's colour, resolved once for the map, the page and the
    export: a caller's override, else the feed's ``route_color``, else the
    default. Keyed by label in the order the lines first appear on an edge,
    so the map's stacking and the page's chips agree. An override for a
    label the graph does not carry is ignored -- a client keeps colours for
    lines a narrower mode has since dropped -- and one that is not
    ``#rrggbb`` is refused."""
    colors: dict[str, str] = {}
    for e in graph.edges:
        for ln in e.lines:
            colors.setdefault(ln.label, _color(ln.color, default))
    for label, value in (overrides or {}).items():
        if label in colors:
            colors[label] = check_color(value, f"the colour of line {label!r}")
    return colors


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
    # The colour each line was drawn in, by label: what the animation page's
    # chips, dots and chart show, so nothing downstream resolves a colour
    # again or falls back on its own.
    colors: dict[str, str] = field(default_factory=dict)
    # The lines' own strokes the tracks were laid with (issue 55), by label:
    # what the geographic twin lays its tracks with, so it pairs with these.
    strokes: dict[str, LineStroke] = field(default_factory=dict)

    def track(self, label: str, src: str, dst: str) -> TrackPath | None:
        """Look up a track path in either direction."""
        return self.tracks.get((label, src, dst)) or self.tracks.get((label, dst, src))


def slot_width(style: Style, strokes: dict[str, LineStroke] | None, label: str) -> float:
    """The room line ``label`` takes across an edge in user units, its casing
    included: ``line_width`` itself for a line with no stroke of its own."""
    stroke = (strokes or {}).get(label)
    return style.line_width * stroke.slot if stroke else style.line_width


def build_tracks(graph: LineGraph, proj: Projection, style: Style,
                 strokes: dict[str, LineStroke] | None = None
                 ) -> dict[tuple[str, str, str], TrackPath]:
    """Every line's track across every edge, pushed off the edge's centre by
    its slot (``offsets.slot_offsets``): ``strokes`` are the lines' own widths
    and casings, by label, and a line without one takes ``line_width``. An
    edge none of whose lines has a stroke is placed as it always was, to the
    bit, so a width moves tracks only on the edges its line runs on.

    A track's element id is its edge's index and its label with every
    character but a letter or a digit made an underscore, so two labels on one
    edge that differ only in those ("A-1", "A 1") would share one, and a
    casing's use would draw the other line's track. A repeat takes the first
    of _2, _3 and on that is free; a map with no repeat has the ids it had."""
    tracks: dict[tuple[str, str, str], TrackPath] = {}
    ids: set[str] = set()
    for ei, edge in enumerate(graph.edges):
        centre = [proj(c) for c in edge.geometry]
        offsets = slot_offsets([slot_width(style, strokes, line.label) for line in edge.lines],
                               style.line_width, style.spacing)
        for i, line in enumerate(edge.lines):
            pts = offset_polyline(centre, offsets[i])
            eid = f"t{ei}_{_safe(line.label)}"
            if eid in ids:
                n = 2
                while f"{eid}_{n}" in ids:
                    n += 1
                eid = f"{eid}_{n}"
            ids.add(eid)
            tracks[(line.label, edge.src, edge.dst)] = TrackPath(
                element_id=eid, label=line.label, src=edge.src, dst=edge.dst, points=pts)
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


# ------------------------------------------------------------------ markers

def _heading(points: list[Coord], reach: float) -> Coord | None:
    """The unit direction from a polyline's first point to its point ``reach``
    along it (its last, if it is shorter), or None where it has no length.
    Read a stretch away rather than off the first segment, because LOOM leaves
    stubs at station nodes far shorter than a line is wide, and their angles
    are rounding noise."""
    pts = dedupe(points)
    if len(pts) < 2:
        return None
    far = point_at(pts, min(reach, cumulative_lengths(pts)[-1]))
    dx, dy = far[0] - pts[0][0], far[1] - pts[0][1]
    length = math.hypot(dx, dy)
    return (dx / length, dy / length) if length > 1e-9 else None


def _through(legs: list[tuple[Coord, Coord]]) -> Coord:
    """The line's direction at a node, as a unit vector in the drawing's
    coordinates, from each edge there: its direction away from the node and
    its direction of travel (``src`` to ``dst``) at the node.

    One edge: its own travel. More: the two most nearly opposite, which are
    the line running straight through or the bend it turns, and the
    direction from one to the other, which is square to the bend's bisector,
    so a marker across it bisects the bend. It runs the way the first of the
    two travels, so its right is that edge's right. Pairs as opposite as each
    other to nine places, as where two lines cross straight through, go to
    the first in edge order: the last bit of a diagonal's unit vector does
    not choose."""
    if len(legs) == 1:
        return legs[0][1]
    pairs = [(i, j) for i in range(len(legs)) for j in range(i + 1, len(legs))]
    i, j = min(pairs, key=lambda p: round(legs[p[0]][0][0] * legs[p[1]][0][0]
                                          + legs[p[0]][0][1] * legs[p[1]][0][1], 9))
    (a, travel), (b, _) = legs[i], legs[j]
    tx, ty = b[0] - a[0], b[1] - a[1]
    length = math.hypot(tx, ty)
    if length < 1e-9:  # out and back along one bearing: across that bearing
        return travel
    tx, ty = tx / length, ty / length
    return (tx, ty) if tx * travel[0] + ty * travel[1] >= 0 else (-tx, -ty)


def _num(value: float) -> str:
    text = f"{value:.2f}"
    return "0.00" if text == "-0.00" else text


@dataclass
class _Marker:
    """One station's marker, as ``Style``'s two shapes make it (issue 74).

    ``size`` is a circle's radius, half a square's side, or a tick's line
    width. ``tangent`` is the line's direction at the station (``_through``)
    and ``side`` the side of it a tick stands on: 1 on its right, -1 on its
    left. A tick at the end of its line (``terminus``) is TfL's double tab,
    drawn as one bar across the line reaching a tick's length past it on
    both sides; there ``tangent`` points out past the end."""

    x: float
    y: float
    shape: str
    size: float
    interchange: bool
    tangent: Coord = (1.0, 0.0)
    terminus: bool = False
    color: str = ""
    side: float = 1.0

    def corners(self, both: bool = False) -> list[Coord]:
        """The outline's four corners in the drawing, a rounded square's
        before its rounding. ``both`` is a tick standing on either side at
        once: the bar a label is kept clear of before its side is known."""
        tx, ty = self.tangent
        rx, ry = -ty, tx
        if self.shape == "tick":
            # From the line's middle to TICK of its width past its edge, so
            # the part outside the line is TfL's square and nothing of the
            # ground shows between the tick and its line. The double tab's
            # outer face is where the line's round cap ends, so the line
            # stops at the bar rather than showing its cap past it.
            tick = TICK * self.size
            reach = self.size / 2 + tick
            if self.terminus:
                u0, u1, near = self.size / 2 - tick, self.size / 2, -reach
            else:
                u0, u1, near = -tick / 2, tick / 2, (-reach if both else 0.0)
            local = [(u0, near), (u1, near), (u1, reach), (u0, reach)]
            local = [(u, v * self.side) for u, v in local]
        else:
            h = self.size
            local = [(-h, -h), (h, -h), (h, h), (-h, h)]
        return [(self.x + u * tx + v * rx, self.y + u * ty + v * ry) for u, v in local]

    def obstacle(self) -> Quad | None:
        """What a label keeps clear of: the outline itself, and a tick's on
        both sides of its line, since the side is its label's; None for a
        circle, which keeps the square ``place`` gives every circle."""
        return None if self.shape == "circle" else Quad.of(self.corners(both=True))

    def face(self, label: Quad) -> None:
        """Stand a tick on its label's side of the line; a label on neither
        side, along the line itself, leaves it on the right. A double tab
        stands on both sides already."""
        if self.terminus:
            return
        cx = sum(p[0] for p in label.pts) / 4 - self.x
        cy = sum(p[1] for p in label.pts) / 4 - self.y
        self.side = -1.0 if -cx * self.tangent[1] + cy * self.tangent[0] < -1e-6 else 1.0

    def path(self) -> str:
        """The outline as path data: a moveto to the station, then the shape
        drawn relative to it. So the station is the first two numbers, and
        moving them moves the marker whole, which is how the page carries it
        between views. Each corner is written to the hundredth, as a circle's
        centre is, and every step is the difference of two written corners."""
        tx, ty = self.tangent
        rx, ry = -ty, tx

        def at(u: float, v: float) -> Coord:
            return (self.x + u * tx + v * rx, self.y + u * ty + v * ry)

        if self.shape == "square" and self.interchange:
            # Rounded by a quarter of the side, clockwise from the top edge.
            h, q = self.size, self.size / 2
            steps = [("m", at(-h + q, -h)), ("l", at(h - q, -h)), ("a", at(h, -h + q)),
                     ("l", at(h, h - q)), ("a", at(h - q, h)), ("l", at(-h + q, h)),
                     ("a", at(-h, h - q)), ("l", at(-h, -h + q)), ("a", at(-h + q, -h))]
        else:
            first, *rest = self.corners()
            steps = [("m", first)] + [("l", p) for p in rest]
        arc = f"a {_num(self.size / 2)} {_num(self.size / 2)} 0 0 1"
        here = (float(f"{self.x:.2f}"), float(f"{self.y:.2f}"))
        out = [f"M {self.x:.2f} {self.y:.2f}"]
        for verb, point in steps:
            snapped = (float(f"{point[0]:.2f}"), float(f"{point[1]:.2f}"))
            out.append(f"{arc if verb == 'a' else verb} "
                       f"{_num(snapped[0] - here[0])} {_num(snapped[1] - here[1])}")
            here = snapped
        return " ".join(out) + " z"


def _markers(graph: LineGraph, style: Style, proj: Projection, node_xy: dict[str, Coord],
             routes_at: dict[str, set[str]], colors: dict[str, str],
             strokes: dict[str, LineStroke] | None = None) -> dict[str, _Marker]:
    """Every station's marker, by node. An interchange is a node more than one
    line runs through, as it always was, and takes ``interchange_shape``;
    every other station takes ``station_shape``. A station no line reaches,
    or whose edges have no length, is a circle whatever the style, since a
    tick or a square has nothing to turn by. The line's direction is only
    worked out when a shape needs it, so a map of circles costs what it did.
    A tick is sized by its line's slot (``slot_width``), so it stands TICK of
    a widened or cased line past that line's edge, as it does a plain one's."""
    adjacency = (graph.adjacency()
                 if (style.station_shape, style.interchange_shape) != ("circle", "circle") else {})
    markers: dict[str, _Marker] = {}
    for node in graph.stations:
        x, y = node_xy[node.id]
        lines = routes_at.get(node.id, set())
        interchange = len(lines) > 1
        radius = style.interchange_radius if interchange else style.station_radius
        shape = style.interchange_shape if interchange else style.station_shape
        # Never a tick where lines meet, nor a shape that is not one, even from
        # a style changed after it was made, which ``Style`` cannot see.
        if shape not in (INTERCHANGE_SHAPES if interchange else STATION_SHAPES):
            shape = "circle"
        legs: list[tuple[Coord, Coord]] = []
        if shape != "circle":
            for edge, _ in adjacency.get(node.id, ()):
                if not edge.lines or edge.src == edge.dst:
                    continue
                pts = [proj(c) for c in edge.geometry]
                away = _heading(pts if edge.src == node.id else pts[::-1], style.line_width)
                if away is not None:
                    legs.append((away, away if edge.src == node.id else (-away[0], -away[1])))
        if not legs:
            markers[node.id] = _Marker(x, y, "circle", radius, interchange)
        elif shape == "tick":
            end = len(legs) == 1
            tangent = (-legs[0][0][0], -legs[0][0][1]) if end else _through(legs)
            line = next(iter(lines))
            markers[node.id] = _Marker(x, y, "tick", slot_width(style, strokes, line), False,
                                       tangent, terminus=end, color=colors[line])
        else:
            markers[node.id] = _Marker(x, y, "square", radius, interchange, _through(legs))
    return markers


def render(graph: LineGraph, *, width: float = 1800.0, style: Style | None = None,
           labels: bool = True, title: str | None = None,
           line_order: list[str] | None = None,
           colors: dict[str, str] | None = None,
           strokes: dict[str, LineStroke] | None = None) -> RenderResult:
    """Draw the graph. ``width`` sizes the network; the canvas grows for labels.

    ``colors`` overrides a line's colour by label, over the feed's own and
    before the style's default (``line_colors``); ``line_order`` is the
    stacking on shared track, the lines it leaves out following the ones
    it names. ``strokes`` is a line's own width, casing and dash, by label
    (``line_strokes``): a widened or cased line takes a wider slot on its
    own edges, and the labels, the markers and the canvas keep clear of it;
    a line without one is drawn as it always was."""
    style = style or Style()
    strokes = strokes or {}
    face = label_face(style.label_font)
    proj = Projection.fit(graph, width)
    tracks = build_tracks(graph, proj, style, strokes)
    node_xy = {nid: proj(n.coord) for nid, n in graph.nodes.items()}

    colors = line_colors(graph, default=style.default_line_color, overrides=colors)
    routes_at: dict[str, set[str]] = {}
    for e in graph.edges:
        for end in (e.src, e.dst):
            routes_at.setdefault(end, set()).update(ln.label for ln in e.lines)

    # A caller's order is a preference, not a whitelist: a line it leaves
    # out is drawn after the ones it names rather than not drawn at all.
    order = ordered_labels(line_order, sorted(colors))
    markers = _markers(graph, style, proj, node_xy, routes_at, colors, strokes)

    # --- labels ---------------------------------------------------------
    placements: list[Placement] = []
    dropped: list[str] = []
    if labels:
        obstacles: list[Quad] = []
        for tp in tracks.values():
            obstacles += polyline_quads(tp.points, slot_width(style, strokes, tp.label))
        # How far the track bundle reaches either side of each station, so its
        # label can clear it: half its slots and the gaps between them, at the
        # widest edge there. An interchange where five lines run together needs
        # a lot more room than a single-track outer stop. With every slot
        # line_width the extra is 0.0, and this is (n - 1) / 2 spacings and
        # half a line for the most lines on an edge, as it always was.
        reach: dict[str, float] = {}
        for e in graph.edges:
            half = ((len(e.lines) - 1) / 2 * style.spacing + style.line_width / 2
                    + sum(slot_width(style, strokes, ln.label) - style.line_width
                          for ln in e.lines) / 2)
            for end in (e.src, e.dst):
                reach[end] = max(reach.get(end, -math.inf), half)

        stations = []
        for node in graph.stations:
            if not node.station_label:
                continue
            x, y = node_xy[node.id]
            stations.append(Station(
                text=display_name(node.station_label), x=x, y=y, key=node.id,
                importance=len(routes_at.get(node.id, ())),
                on_horizontal_run=_horizontal_run(graph, node.id, proj),
                clearance=reach.get(node.id, style.line_width / 2),
                marker=markers[node.id].obstacle(),
            ))
        placements, dropped_stations = place(
            stations, obstacles, size=style.label_size,
            measure=label_measure(style), offset=style.label_offset,
            marker_radius=style.interchange_radius)
        dropped = [s.text for s in dropped_stations]
        # A tick stands toward its station's name, as TfL draws it.
        for p in placements:
            if markers[p.key].shape == "tick":
                markers[p.key].face(p.quad)

    # --- canvas: derive from what is actually drawn ----------------------
    # Two boxes: the network alone, and the network plus its labels. Labels
    # reach well outside the track geometry, so a viewer that hides them is
    # otherwise left staring at a mostly empty canvas.
    net_xs: list[float] = []
    net_ys: list[float] = []
    for tp in tracks.values():
        room = slot_width(style, strokes, tp.label)
        for x, y in tp.points:
            net_xs += [x - room, x + room]
            net_ys += [y - room, y + room]
    for x, y in node_xy.values():
        net_xs += [x - style.interchange_radius, x + style.interchange_radius]
        net_ys += [y - style.interchange_radius, y + style.interchange_radius]
    # A tick or a turned square can reach past that; a circle never does.
    for marker in markers.values():
        if marker.shape != "circle":
            for x, y in marker.corners():
                net_xs.append(x)
                net_ys.append(y)

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
        # A bundled face goes first, the system stack behind it for any
        # character the subset lacks; the system face writes the stack alone.
        f'font-family="{_attr(f"{face.family}, {SYSTEM_FONTS}" if face else SYSTEM_FONTS)}">',
    ]
    if title:
        out.append(f"<title>{html.escape(title)}</title>")
    if face:
        # The face itself, before anything is drawn, so the SVG stays one
        # self-contained file; the system face embeds nothing. Inline in the
        # animation page, the rule reaches the names the page draws too.
        out.append(f"<style>{face.font_face}</style>")
    # Identified so a consumer that changes the viewBox can grow it to match.
    out.append(f'<rect id="backdrop" x="{min_x:.2f}" y="{min_y:.2f}" '
               f'width="{w:.2f}" height="{h:.2f}" '
               f'fill="{_attr(style.var("bg", style.background))}"/>')

    out.append('<g id="lines" fill="none" stroke-linecap="round" stroke-linejoin="round">')
    for label in order:
        if label not in colors:
            continue
        # A line's own stroke, when it has one, is its group's: the width and
        # the dash are read off the group by the page's chips and swatches, as
        # the width always was, and the draw-in's dash on a track gives way to
        # the group's when the track is whole.
        stroke = strokes.get(label)
        t = style.line_width * stroke.width if stroke else style.line_width
        dash = stroke.dasharray(t) if stroke else None
        out.append(f'<g class="line" data-line="{html.escape(label)}" '
                   f'stroke="{_attr(colors[label])}" '
                   f'stroke-width="{t:.2f}"'
                   + (f' stroke-dasharray="{dash}"' if dash else "") + ">")
        if stroke and stroke.casing > 0:
            # The casing: a stroke of the slot's width under each of the line's
            # own tracks, all of them first so no track's casing covers another
            # track's cap, never one per edge, undashed. A use of the track, so
            # whatever the page does to the track's path its casing follows.
            b = slot_width(style, strokes, label)
            for tp in tracks.values():
                if tp.label == label:
                    out.append(f'<use href="#{tp.element_id}" '
                               f'stroke="{_attr(stroke.casing_color)}" '
                               f'stroke-width="{b:.2f}" stroke-dasharray="none"/>')
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

    # One marker a station, each with its node. A circle is written as it
    # always was, with no data-shape; a tick or a square is a path whose first
    # moveto is the station (``_Marker.path``) and says which it is. A tick is
    # in its line's colour and has no outline; a square has a circle's.
    out.append('<g id="stations">')
    for node in graph.stations:
        x, y = node_xy[node.id]
        marker = markers[node.id]
        ids = (f'data-node="{html.escape(node.id)}" '
               f'data-station-id="{html.escape(node.station_id or "")}"')
        if marker.shape == "tick":
            out.append(f'<path d="{marker.path()}" fill="{_attr(marker.color)}" '
                       f'{ids} data-shape="tick"/>')
            continue
        paint = (f'fill="{_attr(style.var("station-fill", style.station_fill))}" '
                 f'stroke="{_attr(style.var("station-stroke", style.station_stroke_color))}" '
                 f'stroke-width="{style.station_stroke:.2f}"')
        if marker.shape == "square":
            out.append(f'<path d="{marker.path()}" {paint} {ids} data-shape="square"/>')
            continue
        r = style.interchange_radius if len(routes_at.get(node.id, ())) > 1 else style.station_radius
        out.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="{r:.2f}" {paint} {ids}/>')
    out.append("</g>")

    if placements:
        out.append(f'<g id="labels" font-size="{style.label_size:.1f}" '
                   f'fill="{_attr(style.var("label", style.label_color))}">')
        for p in placements:
            transform = (f' transform="rotate({p.rotate:.0f} {p.x:.2f} {p.y:.2f})"'
                         if p.rotate else "")
            # A haloed label sits over a line; the stroke is painted behind the
            # glyphs so the name stays legible against the colour.
            halo = (f' stroke="{_attr(style.var("bg", style.background))}" stroke-width="3.2"'
                    f' paint-order="stroke" stroke-linejoin="round"' if p.haloed else "")
            out.append(f'<text x="{p.x:.2f}" y="{p.y:.2f}" text-anchor="{p.anchor}"'
                       f' data-node="{html.escape(p.key)}"'
                       f'{transform}{halo}>{html.escape(p.text)}</text>')
        out.append("</g>")

    out.append('<g id="trains"></g>')
    out.append("</svg>")

    return RenderResult(svg="\n".join(out), width=w, height=h, projection=proj,
                        tracks=tracks, node_xy=node_xy, dropped_labels=dropped,
                        colors=colors, strokes=strokes)


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


# --------------------------------------------------------------------------
# A stage's graph, drawn, for someone else to rely on (E15)
# --------------------------------------------------------------------------

def summary(graph: LineGraph) -> dict:
    """The counts a stage is described by: nodes, stations, junctions, edges
    and the line labels. The protocol's StageSummary."""
    stations = len(graph.stations)
    return {"nodes": len(graph.nodes), "stations": stations,
            "junctions": len(graph.nodes) - stations, "edges": len(graph.edges),
            "lines": list(graph.labels)}


def stage(key: str, stage: str, *, layout: str | None = None, width: float = 1200.0,
          labels: bool = False, date=None, **overrides) -> tuple[str, dict, dict]:
    """One stored stage graph of a feed, as SVG, with its counts and its
    description.

    ``stage`` is one of the pipeline's four (``gtfs2graph``, ``topo``, ``loom``,
    ``octi``). ``layout`` names the stored set by its id; without it, the set
    stored for the registry entry with ``overrides`` applied, as the site and
    the notebooks find it. The graph is reprojected before it is drawn, as
    every drawing here must be: LOOM emits lon/lat and computes in metres.
    A stage that is not stored raises ``pipeline.LayoutMissing`` with a hint
    naming it and what builds it.

    The description is the protocol's StageDescription of the graph drawn
    (``describe.describe``), made after the drawing, which it leaves as it
    was. ``date``, the project's service day, times each line by its
    commonest trip that day (``pipeline.line_minutes``, read once per layout
    and day); without it the minutes are null and no timetable is read, and
    nothing here picks a day.
    """
    from . import pipeline  # here, not at the top: pipeline imports this module

    if stage not in pipeline.STAGE_FILES:
        raise ValueError(f"{stage!r} is not a stage; the stages are "
                         + ", ".join(pipeline.STAGE_FILES))
    if layout is not None:
        found = pipeline.read_layout(key, layout)
    else:
        found = pipeline.stored(key, **overrides)
    if found is None:
        raise pipeline.LayoutMissing(
            f"{key!r} has no stored {stage} graph"
            + (f" under layout {layout[:8]}" if layout else "")
            + "; lay the feed out first (graph.build)")
    path = found.paths[stage]
    if not path.is_file():
        raise pipeline.LayoutMissing(
            f"{key!r} has no stored {stage} graph; lay the feed out first (graph.build)")
    graph = LineGraph.from_geojson(path)
    minutes = pipeline.line_minutes(found, date) if date is not None else None
    return draw_stage(graph, stage, width=width, labels=labels, minutes=minutes)


def draw_stage(graph: LineGraph, stage: str, *, width: float = 1200.0, labels: bool = False,
               minutes: dict | None = None) -> tuple[str, dict, dict]:
    """A stage graph as LOOM wrote it, in lon/lat, drawn as ``stage`` draws a
    stored one: the SVG, the counts and the description, ``minutes`` being
    ``pipeline.line_minutes``' answer or None. ``stage`` finds a stored
    layout's file by its id and calls this; ``serve._drawn_stage`` calls it
    with a graph read from a running build's scratch, so both are drawn one
    way (issue 43)."""
    from .crs import to_mercator
    from .describe import describe

    graph = graph.reproject(to_mercator)
    counts = summary(graph)
    if stage == "octi":
        ok, total = octilinearity(graph)
        counts["octilinear"] = ok / total if total else 0.0
    r = render(graph, width=width, labels=labels, style=Style(themed=True))
    counts["width"] = r.width
    counts["height"] = r.height
    return r.svg, counts, describe(graph, minutes)

