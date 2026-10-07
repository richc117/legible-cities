"""A map's thumbnail pair: the network alone, once per interface palette (issue 51).

The desktop app's front door shows a small picture of each project's map, and
the app draws no map of its own, so the picture is a file the engine wrote.
It is shown in an ``<img>``, where a ``var()`` falls back to its literal and a
ground the file does not paint is whatever the card is -- which is why the
files are drawn with no ground rectangle and why every colour in them is a
literal.

The drawing is ``render`` with ``labels=False``, the same code path as
``site.export_unlabelled`` and as the map itself, so the project's colours
and its line order reach it as they reach the page, and the tracks are the
ones ``render`` lays. Two things differ from that drawing, both for size and both
said here rather than guessed at:

* **It is about 400 units wide, not 1800**, and its strokes are drawn at
  ``STROKE_SCALE`` of the page's rather than at the page's own widths. At
  the page's proportions a line is 0.4 % of the map's width, under a
  device pixel at the width of a card; at ``STROKE_SCALE`` it is 0.7 %, a
  pixel or more. The tracks' spacing follows the line width
  (``Style.spacing``), so a bundle of lines keeps its proportions.
* **It is written compactly.** ``render`` marks every path and station for
  the animation page's use (an id, the nodes it joins) and writes each track
  as an element of its own. None of that is read through an ``<img>``, so
  here a line's tracks are one path, the stations share their paint, and
  coordinates are rounded to one decimal -- a tenth of a unit is a
  four-thousandth of the picture's width, below what either renderer can
  show -- written without the ``.0`` where it is whole.

The furniture colours are the style's own variables, resolved by
``export.resolve`` against ``export.PALETTES``, the one place the dark and
light values live. A line's colour is a literal from the start and is never
touched by the resolution.
"""

from __future__ import annotations

import html
import re
from dataclasses import replace
from pathlib import Path

from .export import PALETTES, resolve
from .linegraph import Coord, LineGraph, ordered_labels
from .render import RenderResult, Style, check_color, render

# The picture's width in its own units, padding and half a stroke either side
# included: the network is drawn to leave that much, and a bundle of parallel
# tracks that reaches past it at an edge of the map makes the picture a few
# units wider, so this is "about 400", which is all the issue asks.
THUMB_WIDTH = 400.0

# The page's strokes, scaled for a picture a fifth of its size: see above.
STROKE_SCALE = 0.4

THEMES = ("dark", "light")

_BOX = re.compile(r'data-viewbox-nolabels="(-?[\d.]+) (-?[\d.]+) ([\d.]+) ([\d.]+)"')


def files_for(key: str) -> dict[str, str]:
    """The names, by the keys ``map.build`` answers them under, of one map's
    two thumbnails: ``<key>-thumb-dark.svg`` and ``<key>-thumb-light.svg``."""
    return {f"thumb_{theme}": f"{key}-thumb-{theme}.svg" for theme in THEMES}


def style(default_color: str | None = None) -> Style:
    """The page's own style at a thumbnail's scale, themed so that the
    furniture's colours are variables ``export.resolve`` can fill in."""
    base = Style(themed=True)
    scaled = replace(
        base,
        line_width=round(base.line_width * STROKE_SCALE, 1),
        station_radius=round(base.station_radius * STROKE_SCALE, 1),
        interchange_radius=round(base.interchange_radius * STROKE_SCALE, 1),
        station_stroke=round(base.station_stroke * STROKE_SCALE, 1),
        padding=round(base.padding * STROKE_SCALE, 1))
    if default_color is not None:
        scaled = replace(scaled, default_line_color=check_color(default_color, "default_color"))
    return scaled


def draw(graph: LineGraph, *, colors: dict[str, str] | None = None,
         default_color: str | None = None,
         line_order: list[str] | None = None) -> dict[str, str]:
    """The thumbnail of a projected graph in each theme, by theme name.

    ``colors``, ``default_color`` and ``line_order`` are what ``map.build``
    takes and ``pipeline.run`` hands to ``render``, and mean the same here.
    The graph is drawn once; the themes differ only in what resolves the
    furniture's variables."""
    s = style(default_color)
    # Sized so that the picture, whose box is the network plus the padding and
    # half a stroke either side, comes to about THUMB_WIDTH.
    inner = THUMB_WIDTH - 2 * (s.padding + s.line_width)
    r = render(graph, width=inner, style=s, labels=False,
               line_order=line_order, colors=colors)
    box = _BOX.search(r.svg)
    if box is None:
        raise ValueError("render left no data-viewbox-nolabels to size the thumbnail by")
    picture = _picture(graph, r, s, [float(n) for n in box.groups()], line_order)
    return {theme: resolve(picture, PALETTES[theme]) for theme in THEMES}


def write(graph: LineGraph, folder: Path, key: str, *, colors: dict[str, str] | None = None,
          default_color: str | None = None,
          line_order: list[str] | None = None) -> dict[str, Path]:
    """Write both thumbnails of ``key``'s map into ``folder``, beside its
    page, and say where, under the keys ``map.build`` answers them with."""
    drawn = draw(graph, colors=colors, default_color=default_color, line_order=line_order)
    folder.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for answer, name in files_for(key).items():
        path = folder / name
        path.write_text(drawn[answer.removeprefix("thumb_")], encoding="utf-8", newline="\n")
        written[answer] = path
    return written


# --------------------------------------------------------------------- drawing


def _esc(value: str) -> str:
    return html.escape(value, quote=True)


def _tenths(value: float) -> int:
    return round(value * 10)


def _num(tenths: int) -> str:
    """A tenth-unit integer as a decimal with at most one place, with no
    ``.0`` on a whole number and no sign on a zero."""
    whole, tenth = divmod(abs(tenths), 10)
    return f"{'-' if tenths < 0 else ''}{whole}" + (f".{tenth}" if tenth else "")


def _path(points: list[Coord]) -> str:
    """``M x y L x y ...``, rounded, with a point left out where rounding put
    it on the one before; nothing for a track that rounds to a point."""
    pts: list[tuple[int, int]] = []
    for x, y in points:
        pt = (_tenths(x), _tenths(y))
        if not pts or pt != pts[-1]:
            pts.append(pt)
    if len(pts) < 2:
        return ""
    return "M" + "L".join(f"{_num(x)} {_num(y)}" for x, y in pts)


def _picture(graph: LineGraph, r: RenderResult, s: Style, box: list[float],
             line_order: list[str] | None) -> str:
    """``r``'s tracks and stations as a picture whose furniture colours are
    still the style's variables: no ground, no ids, a path per line in the
    page's stacking order, the stations' paint said once."""
    _, _, w, h = box
    view = " ".join(_num(_tenths(v)) for v in box)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.0f}" height="{h:.0f}" '
           f'viewBox="{view}">',
           '<g fill="none" stroke-linecap="round" stroke-linejoin="round">']

    by_line: dict[str, list[str]] = {}
    for track in r.tracks.values():
        by_line.setdefault(track.label, []).append(_path(track.points))
    for label in ordered_labels(line_order, sorted(r.colors)):
        d = "".join(by_line.get(label, ()))
        if d:
            out.append(f'<g stroke="{_esc(r.colors[label])}" '
                       f'stroke-width="{_num(_tenths(s.line_width))}"><path d="{d}"/></g>')
    out.append("</g>")

    routes_at: dict[str, set[str]] = {}
    for e in graph.edges:
        for end in (e.src, e.dst):
            routes_at.setdefault(end, set()).update(ln.label for ln in e.lines)
    fill = s.var("station-fill", s.station_fill)
    stroke = s.var("station-stroke", s.station_stroke_color)
    out.append(f'<g fill="{_esc(fill)}" stroke="{_esc(stroke)}" '
               f'stroke-width="{_num(_tenths(s.station_stroke))}">')
    for node in graph.stations:
        x, y = r.node_xy[node.id]
        radius = s.interchange_radius if len(routes_at.get(node.id, ())) > 1 else s.station_radius
        out.append(f'<circle cx="{_num(_tenths(x))}" cy="{_num(_tenths(y))}" '
                   f'r="{_num(_tenths(radius))}"/>')
    out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"
