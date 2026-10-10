"""How a train is drawn: the dot's radius and the trail behind it (issue 75).

``dot_radius`` and ``trail`` are the animation's, not the map's, so they ride
in the page's data and never in the SVG. Each is written only when it is not
the default, the way ``names`` is, so a map built without them carries the
data it always did, byte for byte.
"""

import datetime as dt
import json
import math
import re

import pytest
from test_colors import _graph
from test_lines import stand  # noqa: F401  (the fixture: the store and the feed stood in)

from schematic import animate
from schematic.linegraph import Edge, Line, LineGraph, Node
from schematic.offsets import polyline_length
from schematic.render import Style, line_strokes, render

DAY = dt.date(2026, 9, 10)


def _built(**kwargs) -> animate.Animation:
    graph = _graph([[("A", "0072bc")]])
    return animate.build(render(graph, labels=False), graph, [], DAY, **kwargs)


def test_a_page_built_without_them_carries_neither_key():
    data = _built().to_json()
    assert "dot_radius" not in data and "trail" not in data
    assert set(data) == {"date", "lines", "linear", "paths", "trips"}


@pytest.mark.parametrize("given", [{"dot_radius": 5}, {"dot_radius": 5.0}, {"trail": 0},
                                   {"trail": 0.0}, {"dot_radius": 5, "trail": 0}])
def test_a_value_at_its_default_writes_nothing(given):
    data = _built(**given).to_json()
    assert "dot_radius" not in data and "trail" not in data
    assert data == _built().to_json()


def test_the_defaults_are_the_pages_own_dot_and_no_trail():
    assert (animate.DOT_RADIUS, animate.TRAIL) == (5, 0)
    assert (animate.DOT_RADIUS_RANGE, animate.TRAIL_RANGE) == ((2, 12), (0, 3))


def test_each_key_is_written_alone_and_by_its_name():
    assert _built(dot_radius=8).to_json()["dot_radius"] == 8
    assert "trail" not in _built(dot_radius=8).to_json()
    assert _built(trail=1.5).to_json()["trail"] == 1.5
    assert "dot_radius" not in _built(trail=1.5).to_json()
    both = _built(dot_radius=7.5, trail=3).to_json()
    assert (both["dot_radius"], both["trail"]) == (7.5, 3)


def test_a_whole_number_is_written_as_one(tmp_path):
    graph = _graph([[("A", "0072bc")]])
    drawn = render(graph, labels=False)
    animation = animate.build(drawn, graph, [], DAY, dot_radius=8.0, trail=2.0)
    positions, page = animate.write(animation, drawn.svg, tmp_path, stem="whole")
    text = positions.read_text(encoding="utf-8")
    assert '"dot_radius":8,' in text and '"trail":2,' in text
    element = re.search(r'<script id="data" type="application/json">(.*?)</script>',
                        page.read_text(encoding="utf-8"), re.S).group(1)
    data = json.loads(element)
    assert (data["dot_radius"], data["trail"]) == (8, 2)


def test_the_page_and_the_positions_file_a_map_had_are_as_they_were(tmp_path):
    """A map built without the two numbers, or with them at their defaults,
    writes the page and the positions file it wrote before, byte for byte."""
    graph = _graph([[("A", "0072bc")], [("A", "0072bc")]])
    drawn = render(graph)
    plain = animate.build(drawn, graph, [], DAY)
    explicit = animate.build(drawn, graph, [], DAY, dot_radius=5, trail=0)
    chosen = animate.build(drawn, graph, [], DAY, dot_radius=9, trail=1)
    written = {}
    for name, animation in (("plain", plain), ("explicit", explicit), ("chosen", chosen)):
        positions, page = animate.write(animation, drawn.svg, tmp_path / name, stem="p")
        written[name] = (positions.read_bytes(), page.read_bytes())
    assert written["explicit"] == written["plain"]
    assert written["chosen"][0] != written["plain"][0]
    # What differs is the two keys and nothing else.
    without = json.loads(written["chosen"][0])
    assert without.pop("dot_radius") == 9 and without.pop("trail") == 1
    assert without == json.loads(written["plain"][0])


def test_the_pipeline_hands_the_dot_and_the_trail_to_the_page(stand, tmp_path):  # noqa: F811
    """Through ``pipeline.run`` over a stored layout stood in: the two numbers
    reach the page's data and nothing else, and at their defaults, sent or not,
    the map is the one drawn without them."""
    plain = stand.run(tmp_path / "plain", date=DAY)
    result, svg, data, page = stand.run(tmp_path / "drawn", date=DAY,
                                        dot_radius=8, trail=1.5)
    assert (result.animation.dot_radius, result.animation.trail) == (8, 1.5)
    assert (data["dot_radius"], data["trail"]) == (8, 1.5)
    assert animate._json_for_script(data) in page
    assert svg == plain[1]
    assert {k: v for k, v in data.items() if k not in ("dot_radius", "trail")} == plain[2]
    explicit = stand.run(tmp_path / "explicit", date=DAY, dot_radius=5, trail=0)
    assert (explicit[1], explicit[2], explicit[3]) == (plain[1], plain[2], plain[3])
    assert "dot_radius" not in plain[2] and "trail" not in plain[2]


# ------------------------------------------------ a node's hop (issue 55)
#
# The map joins no two tracks at a node: each is its own round-capped path.
# Where a line's two ends there differ, a trip's path on a map drawn with a
# line's own stroke keeps the hop between them, counted in its length, instead
# of cutting a chord from the one end to the next track's second point.

X = Line(id="X", label="X", color="#0072bc")
Y = Line(id="Y", label="Y", color="#d6322f")
# n0 -X,Y- n1 -X- n2, and Y turns off at n1 to n3: X sits beside Y on the first
# edge and alone on the second, so its ends at n1 differ.
SPOTS = {"n0": (0.0, 0.0), "n1": (10.0, 0.0), "n2": (20.0, 0.0), "n3": (10.0, -8.0)}
WIDE = {"X": {"width": 1.5, "casing": {"width": 0.5, "color": "#101010"}, "dash": "dashed"},
        "Y": {"width": 1.5, "casing": {"width": 0.25, "color": "#f0e0c0"}}}


def _hopton(spots=SPOTS) -> LineGraph:
    nodes = {n: Node(id=n, coord=xy, station_id=n, station_label=f"Stop {n[1:]}")
             for n, xy in spots.items()}
    return LineGraph(nodes=nodes, edges=[
        Edge("n0", "n1", [spots["n0"], spots["n1"]], [X, Y]),
        Edge("n1", "n2", [spots["n1"], spots["n2"]], [X]),
        Edge("n1", "n3", [spots["n1"], spots["n3"]], [Y])])


def _ride_x(hops: bool, strokes=None):
    graph = _hopton()
    drawn = render(graph, labels=False, strokes=line_strokes(strokes))
    net = animate.RouteNetwork.build(drawn)
    return animate.build_trip_path(net, ["n0", "n1", "n2"], "X", hops=hops), drawn


def test_a_trip_keeps_the_hop_at_a_node_and_counts_it():
    """X ends its first track 9.1 off the centre (beside Y, both widened and
    cased) and starts its second on it: the trip runs to the first end, steps
    the 9.1 across the node, and rides the second track, and the distance to
    n2 counts the step."""
    tp, drawn = _ride_x(True, WIDE)
    first, second = drawn.track("X", "n0", "n1").points, drawn.track("X", "n1", "n2").points
    assert math.dist(first[-1], second[0]) == pytest.approx(9.1, abs=1e-9)
    assert tp.points == first + second
    assert tp.stop_lengths == pytest.approx(
        [0.0, polyline_length(first), polyline_length(first) + 9.1 + polyline_length(second)])
    assert tp.stop_lengths[-1] == pytest.approx(polyline_length(tp.points))


def test_without_the_hop_the_trip_is_the_chord_it_always_was():
    """Asked for no hop, the walk is today's: the run from the first track's
    end goes straight to the second's far point, and the step is not counted.
    And a line whose ends meet at every node has no hop to keep, so the two
    walks are one there."""
    tp, drawn = _ride_x(False, WIDE)
    first, second = drawn.track("X", "n0", "n1").points, drawn.track("X", "n1", "n2").points
    assert tp.points == first + second[1:]
    assert tp.stop_lengths[-1] == pytest.approx(polyline_length(first) + polyline_length(second))
    straight = LineGraph(nodes={n: Node(id=n, coord=SPOTS[n], station_id=n) for n in
                                ("n0", "n1", "n2")},
                         edges=[Edge("n0", "n1", [SPOTS["n0"], SPOTS["n1"]], [X]),
                                Edge("n1", "n2", [SPOTS["n1"], SPOTS["n2"]], [X])])
    net = animate.RouteNetwork.build(render(straight, labels=False))
    kept = animate.build_trip_path(net, ["n0", "n1", "n2"], "X", hops=True)
    cut = animate.build_trip_path(net, ["n0", "n1", "n2"], "X", hops=False)
    assert (kept.points, kept.stop_lengths) == (cut.points, cut.stop_lengths)


def test_the_geographic_twin_makes_room_for_a_widened_line_as_the_map_does():
    """The twin's tracks are laid with the map's strokes: on the shared edge
    X's and Y's twins sit apart by as much more than with no stroke as X and Y
    do on the map. By the ratio, since the twin is fitted to the map's box."""
    graph = _hopton()
    moved = {n: (x + 0.3, y - 0.2) for n, (x, y) in SPOTS.items()}
    twin = _hopton(moved)
    strokes = line_strokes(WIDE)
    drawn = render(graph, labels=False, strokes=strokes)
    plain = render(graph, labels=False)
    wide_geo = animate.geographic_tracks(twin, graph, drawn, Style())
    plain_geo = animate.geographic_tracks(twin, graph, plain, Style())
    gap = lambda tracks, a, b: math.dist(tracks[a][0], tracks[b][0])
    x_a, y_a = drawn.track("X", "n0", "n1").element_id, drawn.track("Y", "n0", "n1").element_id
    on_map = [math.dist(r.tracks[("X", "n0", "n1")].points[0],
                        r.tracks[("Y", "n0", "n1")].points[0]) for r in (drawn, plain)]
    assert on_map == pytest.approx([19.95, 11.2])
    assert gap(wide_geo.tracks, x_a, y_a) / gap(plain_geo.tracks, x_a, y_a) == pytest.approx(
        on_map[0] / on_map[1], rel=0.03)


def test_the_pipeline_keeps_the_hop_only_on_a_map_with_a_stroke(stand, tmp_path):  # noqa: F811
    """Through ``pipeline.run``: a map with a line's own stroke keeps each
    node's hop in its trips' paths; the same map without one, or with only
    a name, writes the data it always wrote (engine issue 70 stays open for
    those, whose pinned data would move)."""
    plain = stand.run(tmp_path / "plain", date=DAY)
    named = stand.run(tmp_path / "named", date=DAY, lines={"A": {"name": "Airport"}})
    assert named[2] == {**plain[2], "names": {"A": "Airport"}}
    styled = stand.run(tmp_path / "styled", date=DAY, lines={"B": {"width": 1.5}})
    hops = animate.build_trip_path(animate.RouteNetwork.build(styled[0].render),
                                   ["n0", "n1", "n2", "n3"], "A", hops=True)
    assert any(p["route"] == "A" and p["d"] == hops.path_d() for p in styled[2]["paths"])
    assert [p["d"] for p in styled[2]["paths"]] != [p["d"] for p in plain[2]["paths"]]
