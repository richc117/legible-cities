"""The thumbnail pair ``map.build`` writes beside the page (issue 51).

The desktop app's front door shows a small picture of each project's map in
an ``<img>``, which resolves a ``var()`` to its fallback and has no ground of
its own. So the pictures are drawn by the engine, with no ground, no labels
and every colour a literal, in the project's colours and line order, once for
each of the interface's two palettes.

These draw hand-made graphs, so they run without ``data/`` or LOOM. What
needs a real network -- the size of New York's pair, the page, SVG and
positions file being byte-identical -- is measured on ``main`` with the
stored layouts (``test_colors.py`` pins the hashes).
"""

import copy
import re

import pytest
from test_colors import DEFAULT, _graph, strokes

from schematic import export, thumbnail
from schematic.linegraph import LineGraph
from schematic.render import Style, render

THEMES = ("dark", "light")


def thumb_strokes(svg: str) -> list[str]:
    """Each line's colour, in the order the picture stacks the lines."""
    return re.findall(r'<g stroke="(#[0-9a-fA-F]{6})" stroke-width', svg)


def three_lines() -> LineGraph:
    """A -> blue, B -> no colour (the default), C -> red; B and C meet A at
    stations, so the middle one is an interchange."""
    return _graph([[("A", "0072bc"), ("B", None)],
                   [("B", None), ("C", "ff0000"), ("A", "0072bc")],
                   [("C", "ff0000")]])


LABELS = ["A", "B", "C", "D", "E", "F", "G"]
COLOURS = ["0072bc", "eb131b", "00933c", "ff6319", None, "b933ad", "996633"]


def grid(n: int) -> LineGraph:
    """An ``n`` by ``n`` network of stations, 100 apart, joined to the
    neighbours east and south by edges of one to four lines, every third of
    them with a diagonal jog, so there are bends, bundles and interchanges."""
    feats = [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [i * 100.0, j * 100.0]},
              "properties": {"id": f"n{i}_{j}", "station_id": f"S{i}_{j}",
                             "station_label": f"Stop {i} {j}"}}
             for i in range(n) for j in range(n)]
    k = 0
    for i in range(n):
        for j in range(n):
            for di, dj in ((1, 0), (0, 1)):
                if i + di >= n or j + dj >= n:
                    continue
                k += 1
                a = [i * 100.0, j * 100.0]
                b = [(i + di) * 100.0, (j + dj) * 100.0]
                mid = [(a[0] + b[0]) / 2 + 12.5, (a[1] + b[1]) / 2]
                coords = [a, b] if k % 3 else [a, mid, [mid[0], mid[1] + 25.0], b]
                picks = [(k + m) % len(LABELS) for m in range(1 + k % 4)]
                lines = [{"id": LABELS[p], "label": LABELS[p],
                          **({"color": COLOURS[p]} if COLOURS[p] else {})} for p in picks]
                feats.append({"type": "Feature",
                              "geometry": {"type": "LineString", "coordinates": coords},
                              "properties": {"from": f"n{i}_{j}", "to": f"n{i + di}_{j + dj}",
                                             "lines": lines}})
    return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})


# ------------------------------------------------------ the files and colours

def test_both_files_are_written_beside_the_page_in_the_projects_colours_and_order(tmp_path):
    graph = three_lines()
    # The feed's own colours and the engine's order, with nothing chosen.
    plain = thumbnail.draw(graph)
    assert thumb_strokes(plain["dark"]) == thumb_strokes(plain["light"]) == \
        ["#0072bc", DEFAULT, "#ff0000"]

    files = thumbnail.write(graph, tmp_path, "demo", colors={"A": "#123456"},
                            default_color="#abcdef", line_order=["C", "A"])
    assert files == {"thumb_dark": tmp_path / "demo-thumb-dark.svg",
                     "thumb_light": tmp_path / "demo-thumb-light.svg"}
    for theme in THEMES:
        svg = files[f"thumb_{theme}"].read_text(encoding="utf-8")
        # A's override, B's new default, C untouched; the order is the
        # caller's, with the line it left out after the ones it named.
        assert thumb_strokes(svg) == ["#ff0000", "#123456", "#abcdef"]
        assert "#0072bc" not in svg
        assert svg == thumbnail.draw(graph, colors={"A": "#123456"}, default_color="#abcdef",
                                     line_order=["C", "A"])[theme]


def test_the_thumbnail_is_made_in_the_folder_it_is_asked_for(tmp_path):
    files = thumbnail.write(three_lines(), tmp_path / "out" / "p1", "demo")
    assert all(path.is_file() and path.parent == tmp_path / "out" / "p1" for path in files.values())


def test_a_colour_that_is_not_rrggbb_is_refused_before_anything_is_drawn(tmp_path):
    with pytest.raises(ValueError, match="default_color must be a colour written #rrggbb"):
        thumbnail.write(three_lines(), tmp_path, "demo", default_color="red")
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------- literals, no ground, no labels

@pytest.mark.parametrize("theme", THEMES)
def test_a_thumbnail_has_no_variable_no_ground_and_no_label(theme):
    svg = thumbnail.draw(three_lines())[theme]
    palette = export.PALETTES[theme]
    assert "var(" not in svg
    assert "<text" not in svg and "Stop" not in svg
    assert "<rect" not in svg and "backdrop" not in svg
    # The stations carry this palette's colours, not the style's fallbacks
    # (which are the light ones) -- the reason for resolving rather than
    # leaving the fallback to an <img>.
    assert f'fill="{palette["station-fill"]}" stroke="{palette["station-stroke"]}"' in svg


def test_the_two_themes_differ_in_the_furniture_and_nowhere_else():
    drawn = thumbnail.draw(three_lines())
    assert drawn["dark"] != drawn["light"]

    def blanked(theme: str) -> str:
        palette = export.PALETTES[theme]
        return drawn[theme].replace(palette["station-fill"], "@FILL").replace(
            palette["station-stroke"], "@STROKE")

    assert "@FILL" in blanked("dark") and blanked("dark") == blanked("light")


@pytest.mark.parametrize("theme", THEMES)
def test_resolving_a_palette_leaves_every_line_colour_alone(theme):
    """The contract the thumbnails lean on, on the page's own drawing: the
    furniture's variables become this palette's literals and a line's
    stroke is the agency's colour before and after."""
    themed = render(three_lines(), labels=False, style=Style(themed=True),
                    colors={"A": "#123456"}).svg
    resolved = export.resolve(themed, export.PALETTES[theme])
    assert "var(" in themed and "var(" not in resolved
    assert export.PALETTES[theme]["station-fill"] in resolved
    assert strokes(resolved) == strokes(themed) == {"A": "#123456", "B": DEFAULT, "C": "#ff0000"}
    # And through the thumbnail, which resolves its whole picture.
    assert thumb_strokes(thumbnail.draw(three_lines(), colors={"A": "#123456"})[theme]) == \
        ["#123456", DEFAULT, "#ff0000"]


# --------------------------------------------------------------- size and shape

def test_a_thumbnail_is_about_400_units_wide_with_one_decimal_coordinates():
    svg = thumbnail.draw(grid(8))["dark"]
    head = re.match(r'<svg xmlns="[^"]+" width="(\d+)" height="(\d+)" '
                    r'viewBox="(-?[\d.]+) (-?[\d.]+) ([\d.]+) ([\d.]+)">', svg)
    assert head, svg[:200]
    width, height, x, y, w, h = (float(g) for g in head.groups())
    # About 400: the network, its padding and half a stroke either side, and
    # a few units more where a bundle of parallel tracks reaches past that.
    assert 400.0 <= w <= 420.0 and abs(width - w) <= 0.5
    assert abs(h - height) <= 0.5
    # No number carries a second decimal, and none a ".0".
    numbers = re.findall(r"-?\d+(?:\.\d+)?", re.sub(r'xmlns="[^"]+"|#[0-9a-fA-F]{6}', "", svg))
    assert numbers and all(re.fullmatch(r"-?\d+(\.[1-9])?", n) for n in numbers), \
        [n for n in numbers if not re.fullmatch(r"-?\d+(\.[1-9])?", n)][:5]


def test_a_thumbnail_is_small_for_what_it_draws():
    """The bound scales with the drawing and is the most it can take: 12 bytes
    for a point ("L999.9 999.9" is the longest a coordinate under a thousand
    writes at one decimal), 40 for a station, 60 for a line's group, and 600
    for the head and the station group. It leaves room for coordinates and
    none for what ``render`` adds per element for the animation page (an id,
    the nodes a path joins), which is why New York's thumbnail is tens of
    kilobytes where the page's SVG is hundreds."""
    graph = grid(12)
    r = render(graph, labels=False, style=thumbnail.style())
    points = sum(len(t.points) for t in r.tracks.values())
    bound = 12 * points + 40 * len(graph.stations) + 60 * len(r.colors) + 600
    for theme, svg in thumbnail.draw(graph).items():
        size = len(svg.encode())
        assert size < bound, f"{theme}: {size} bytes against a bound of {bound}"
    # The bound is not slack: the page's own markup for this graph is over it.
    assert len(r.svg.encode()) > 3 * bound


@pytest.mark.parametrize("graph,has_both_kinds", [(three_lines(), True), (grid(5), False)],
                         ids=["chain", "grid"])
def test_the_picture_is_the_pages_own_drawing_rounded(graph, has_both_kinds):
    """The tracks and stations are ``render``'s, at the thumbnail's width and
    strokes: every point of every line and every station lands within the
    rounding of where the page's SVG puts it, an interchange is drawn with
    the interchange radius, and a line's tracks are one path in the page's
    order."""
    s = thumbnail.style()
    inner = thumbnail.THUMB_WIDTH - 2 * (s.padding + s.line_width)
    page = render(graph, width=inner, style=s, labels=False).svg
    svg = thumbnail.draw(graph)["dark"]

    page_lines = re.findall(r'<g class="line" data-line="([^"]+)" stroke="([^"]+)" '
                            r'stroke-width="([^"]+)">(.*?)</g>', page, re.S)
    ours = re.findall(r'<g stroke="([^"]+)" stroke-width="([^"]+)"><path d="([^"]+)"/></g>', svg)
    assert [(c, float(wd)) for _, c, wd, _ in page_lines] == [(c, float(wd)) for c, wd, _ in ours]
    for (_, _, _, tracks), (_, _, d) in zip(page_lines, ours):
        want = [[float(n) for n in re.findall(r"-?[\d.]+", t)]
                for t in re.findall(r' d="([^"]+)"', tracks)]
        got = [[float(n) for n in re.findall(r"-?[\d.]+", t)] for t in d.split("M")[1:]]
        assert len(want) == len(got)
        for w, g in zip(want, got):
            assert len(w) == len(g) and all(abs(a - b) <= 0.051 for a, b in zip(w, g))

    want_stations = re.findall(r'<circle cx="([^"]+)" cy="([^"]+)" r="([^"]+)"', page)
    got_stations = re.findall(r'<circle cx="([^"]+)" cy="([^"]+)" r="([^"]+)"/>', svg)
    assert len(want_stations) == len(got_stations) == len(graph.stations)
    routes: dict[str, set[str]] = {}
    for e in graph.edges:
        for end in (e.src, e.dst):
            routes.setdefault(end, set()).update(ln.label for ln in e.lines)
    for node, w, g in zip(graph.stations, want_stations, got_stations):
        assert all(abs(float(a) - float(b)) <= 0.051 for a, b in zip(w[:2], g[:2]))
        assert g[2] == _n(s.interchange_radius if len(routes[node.id]) > 1 else s.station_radius)
    if has_both_kinds:
        # The chain has stops and interchanges, so the rule is exercised both ways.
        assert {r for _, _, r in got_stations} == {_n(s.station_radius), _n(s.interchange_radius)}


def _n(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


# ----------------------------------------------------------------- the guard

def test_drawing_a_thumbnail_changes_neither_the_graph_nor_the_callers_choices():
    """Nothing the pair needs is taken from the page's own drawing: the graph
    and what the caller passed are as they were, and ``render`` gives the same
    bytes for the same input before and after, so the page, the SVG and the
    positions file ``map.build`` wrote first are the ones it would have
    written alone."""
    graph = grid(6)
    colors, order = {"A": "#123456", "B": "#654321"}, ["D", "C", "A"]
    before = render(graph, colors=colors, line_order=order).svg
    untouched = copy.deepcopy((graph, colors, order))
    thumbnail.draw(graph, colors=colors, default_color="#abcdef", line_order=order)
    assert (graph, colors, order) == untouched
    assert render(graph, colors=colors, line_order=order).svg == before
