"""Two pictures of the map's themes, for the desktop app to choose by.

The app's style cell offers the map's two themes, Warm dark and Sepia, as two
small pictures rather than two words. The app never draws a map, so the
pictures are the engine's: ``bin/theme-thumbnails <folder>`` writes
``theme-warm-dark.svg`` and ``theme-sepia.svg`` and a ``README.md`` saying how
they were made, and the app ships the files. They show what a theme looks
like, not any project's map (app issue 285, engine issue 53).

The fixture network
-------------------

Settled on 6 Oct 2026 by the design research, and quoted here so nobody has
to rediscover it:

    twelve stations on an invented 16 by 10 grid, like no city: line A west to
    east along y 5, at x 1, 5, 8, 11, 15; B north to south along x 5, at y 9,
    5, 1; C, the one 45° run, at (8, 8), (11, 5), (14, 2); D north to south
    along x 11, at y 9, 5, 3, 1. Interchanges at (5, 5) and (11, 5); one pair
    of ends per line. Route colours A #d6322f, B #2f6fd6, C #1e9150, D
    #c2690f, each at least 3.46:1 on both grounds.

It is drawn *directly*: the grid is already octilinear, so the graph is built
here as a ``LineGraph`` with those positions and handed to ``render.render``.
Running it through LOOM would only move what is already in place, and this way
the script needs no Docker, no ``data/`` and no network, and its output does
not depend on a LOOM build -- a drawing from a fixed graph by a fixed renderer
is the same bytes every time, which a stored layout of a real city is not
(ADR-023).

Scale
-----

One grid unit is 32 drawing units (``SCALE``), so the network is 448 wide. The
renderer adds its own 24 of padding and the overshoot of a line and of an
interchange, which makes the drawing's own viewBox 510 by 318 -- under the 560
the design research allowed, so that at 224 px wide the lines are 3 px and an
interchange about 5 px across. Reframed to 16:10 it is 510.00 by 318.75.

The two files
-------------

Each is the one drawing, resolved through ``export.resolve`` with the palette
of its theme (``PALETTES["dark"]``, ``PALETTES["light"]``) and so carries no
custom property and no script, and none of the page's text (there are no
labels). The viewBox is padded to 16:10, evenly on the short side and never
cropped, so no station is cut; the ground rectangle that comes first covers
the whole of it and is the palette's own ``bg``: that is what the viewer and
every export show. (The page's ``--map-bg`` is its card's ground, a little
lighter in the dark theme, and is the wrong thing to match here.)

The two files differ only in the colours the palettes resolve and in the
ground. Nothing in them says when they were made -- the date is in the
README -- so running the script again changes no byte of either.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
from pathlib import Path

from . import __version__
from .export import PALETTES, padded_box, resolve
from .linegraph import Edge, Line, LineGraph, Node
from .render import Style, render

# The files, and the palette each is resolved with.
THEMES = {
    "theme-warm-dark.svg": "dark",
    "theme-sepia.svg": "light",
}
THEME_NAMES = {"dark": "Warm dark", "light": "Sepia"}

ASPECT = 16 / 10
# Nominal size of the file; a picture shown in an <img> scales from the viewBox.
SIZE = (320, 200)

# One grid unit in drawing units, and the grid's width in units: the renderer
# is asked for the network's width and sizes the rest from it.
SCALE = 32
GRID_WIDTH = 14        # x runs 1 to 15

# The four lines, west to east or north to south, with the colour each is
# drawn in. A line's stations are in order along it.
LINES: dict[str, tuple[str, list[tuple[int, int]]]] = {
    "A": ("#d6322f", [(1, 5), (5, 5), (8, 5), (11, 5), (15, 5)]),
    "B": ("#2f6fd6", [(5, 9), (5, 5), (5, 1)]),
    "C": ("#1e9150", [(8, 8), (11, 5), (14, 2)]),
    "D": ("#c2690f", [(11, 9), (11, 5), (11, 3), (11, 1)]),
}

_VIEWBOX = re.compile(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"')
_SIZED = re.compile(r'width="[\d.]+" height="[\d.]+"')
_BACKDROP = re.compile(r'<rect id="backdrop"[^>]*?/>')


def _node_id(x: int, y: int) -> str:
    return f"x{x}y{y}"


def fixture() -> LineGraph:
    """The invented network, built in code with its positions already set.

    North is up: ``render`` flips y, as it does for a real city's graph.
    """
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []
    for label, (color, stops) in LINES.items():
        for x, y in stops:
            nid = _node_id(x, y)
            # Named, though nothing draws the names: a drawing made with
            # labels on would show them, and the tests rely on that to see
            # that it was not.
            nodes.setdefault(nid, Node(id=nid, coord=(float(x), float(y)),
                                       station_id=nid, station_label=f"Stop {x},{y}"))
        for a, b in zip(stops, stops[1:]):
            edges.append(Edge(
                src=_node_id(*a), dst=_node_id(*b),
                geometry=[(float(a[0]), float(a[1])), (float(b[0]), float(b[1]))],
                lines=[Line(id=label, label=label, color=color)]))
    return LineGraph(nodes=nodes, edges=edges)


def draw(width: float = GRID_WIDTH * SCALE) -> str:
    """The fixture as the renderer draws it, furniture themed and no labels:
    the one drawing both files are made from."""
    return render(fixture(), width=width, style=Style(themed=True), labels=False).svg


def frame(svg: str, theme: str) -> str:
    """``svg`` resolved in ``theme``'s palette and reframed to 16:10.

    Padded, never cropped, and evenly: ``frame_top=0.5`` puts half of any
    extra height above the network and half below. The backdrop is grown to
    the new box and filled with the palette's ``bg``, so the gutters are
    ground rather than transparent.
    """
    palette = PALETTES[theme]
    svg = resolve(svg, palette)

    found = _VIEWBOX.search(svg)
    if not found:
        raise ValueError("the drawing has no viewBox to reframe")
    drawn = tuple(float(v) for v in found.groups())
    x, y, w, h = (round(v, 2) for v in padded_box(drawn, ASPECT, frame_top=0.5))

    svg, n = _VIEWBOX.subn(f'viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}"', svg, count=1)
    svg, m = _SIZED.subn(f'width="{SIZE[0]}" height="{SIZE[1]}"', svg, count=1)
    svg, k = _BACKDROP.subn(
        f'<rect id="backdrop" x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" '
        f'height="{h:.2f}" fill="{palette["bg"]}"/>', svg, count=1)
    # The renderer's own markup is what these patterns read; if it changes
    # shape, say so rather than write a picture with the wrong ground.
    if (n, m, k) != (1, 1, 1):
        raise ValueError("the renderer's drawing no longer has the size, "
                         "viewBox and backdrop this reframing reads")
    if "var(" in svg:
        raise ValueError("a themed value was left unresolved")
    return svg


def header() -> str:
    """The comment that opens each file. The same in both, and with no date,
    so that the two files differ only in their colours and a second run
    writes the same bytes."""
    return (f"<!-- Drawn by the schematic engine {__version__} with "
            f"bin/theme-thumbnails: an invented network of four lines, the "
            f"same drawing in both themes, colours resolved to literals. -->")


def thumbnails() -> dict[str, str]:
    """Each file's text, by file name."""
    drawing = draw()
    return {name: f"{header()}\n{frame(drawing, theme)}\n"
            for name, theme in THEMES.items()}


def readme(today: dt.date, view_box: str) -> str:
    """What sits beside the pictures: what they are, which engine made them
    and when, and how to make them again. The date is here and not in the
    SVGs, whose bytes then stay the same from run to run."""
    rows = "\n".join(
        f"| `{name}` | {THEME_NAMES[theme]} | `{PALETTES[theme]['bg']}` |"
        for name, theme in THEMES.items())
    return f"""# Theme thumbnails

Two pictures of the map's two themes, for the desktop app's style cell to
choose by. The app never draws a map, so these are the engine's.

| File | Theme | Ground |
| --- | --- | --- |
{rows}

Made by engine {__version__} on {today.isoformat()}.

They show what a theme looks like, not any project's map. Both are the same
drawing of an invented network (four lines, twelve stations, no labels, on a
16 by 10 grid like no city), drawn directly by the engine's renderer, with
the furniture's colours resolved to literals so they hold up inside an
`<img>`. The ground is the palette's own `bg`, which is what the viewer and
every export show.

The viewBox is 16:10, padded and never cropped:

    viewBox="{view_box}"

## Making them again

From a checkout of the engine at the tag the app pins:

    bin/theme-thumbnails <folder>

It needs no Docker, no feed and no `data/` folder, and it writes these three
files into the folder, which it creates if it is missing. The SVGs carry no
date, so a second run changes only the line above that names it. Run it again
when the engine's drawing or the palettes change: the files do not follow
them on their own.
"""


def write(folder: Path, today: dt.date | None = None) -> list[Path]:
    """Write the two pictures and their README into ``folder``, which is
    created if it is missing. Returns what it wrote."""
    files = thumbnails()
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, svg in files.items():
        written.append(folder / name)
        written[-1].write_text(svg, encoding="utf-8", newline="\n")
    view_box = _VIEWBOX.search(next(iter(files.values())))
    assert view_box is not None
    note = folder / "README.md"
    note.write_text(readme(today or dt.date.today(), " ".join(view_box.groups())),
                    encoding="utf-8", newline="\n")
    return [*written, note]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bin/theme-thumbnails",
        description="Write theme-warm-dark.svg, theme-sepia.svg and a README "
                    "into a folder.")
    parser.add_argument("folder", type=Path,
                        help="where to write them; created if it is missing")
    args = parser.parse_args(argv)
    try:
        written = write(args.folder)
    except (OSError, ValueError) as exc:
        print(f"bin/theme-thumbnails: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
