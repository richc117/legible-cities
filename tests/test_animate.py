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
from schematic.offsets import point_at, polyline_length
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


def _hopton(spots=SPOTS, turn: list | None = None) -> LineGraph:
    """Hopton; ``turn`` is the geometry of Y's way south from n1, which ends
    at n3, straight from n1 when it is not given."""
    spots = dict(spots)
    if turn:
        spots["n3"] = turn[-1]
    nodes = {n: Node(id=n, coord=xy, station_id=n, station_label=f"Stop {n[1:]}")
             for n, xy in spots.items()}
    return LineGraph(nodes=nodes, edges=[
        Edge("n0", "n1", [spots["n0"], spots["n1"]], [X, Y]),
        Edge("n1", "n2", [spots["n1"], spots["n2"]], [X]),
        Edge("n1", "n3", [spots["n1"], *(turn or [])[1:-1], spots["n3"]], [Y])])


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


def _places(samples, tracks) -> list[tuple[float, float]]:
    """Where each of a train's samples is along a run of tracks laid end to end
    in the order it rides them: the distance from the run's start to the
    sample's foot on the nearest track, and how far the sample is from it.
    Read from the track the last sample was on, the one before it and the two
    after it, so a line that passes near itself elsewhere does not count; of
    two places as near, the later."""
    starts, base = [], 0.0
    for track in tracks:
        starts.append(base)
        base += polyline_length(track)
    out, current = [], 0
    for q in samples:
        best = None
        for t in range(max(0, current - 1), min(len(tracks), current + 3)):
            walked = starts[t]
            for (ax, ay), (bx, by) in zip(tracks[t], tracks[t][1:]):
                dx, dy = bx - ax, by - ay
                length = math.hypot(dx, dy)
                if length == 0:
                    continue
                k = max(0.0, min(length, ((q[0] - ax) * dx + (q[1] - ay) * dy) / length))
                off = math.hypot(q[0] - ax - dx * k / length, q[1] - ay - dy * k / length)
                if best is None or off < best[1] - 1e-9 or (off <= best[1] + 1e-9
                                                           and walked + k > best[0]):
                    best = (walked + k, off, t)
                walked += length
        out.append(best[:2])
        current = best[2]
    return out


def _samples(points) -> list:
    """A path's points every unit along it, and its end."""
    return [point_at(points, s) for s in range(int(polyline_length(points)) + 1)] + [points[-1]]


def _falls(places) -> list[tuple[int, float, float]]:
    return [(i, a, b) for i, ((a, _), (b, _)) in enumerate(zip(places, places[1:]))
            if b < a - 1e-6]


# Y's way south from n1, in Hopton's own units (90 to the drawing's unit):
# straight; with a stub of 2 drawn units at n1, as LOOM leaves at stations; and
# with the same stub and then a bend of 45 degrees, so Y's foot is off its end.
STUB = 2 / 90
TURNS = {"straight": None,
         "stub": [(10.0, 0.0), (10.0, -STUB), (10.0, -8.0)],
         "bend": [(10.0, 0.0), (10.0, -STUB), (17.0, -STUB - 7.0)]}


@pytest.mark.parametrize("turn", list(TURNS))
def test_a_train_never_backs_up_where_its_line_turns_at_a_node(turn):
    """Y runs beside X from n0 and turns south at n1, where it runs alone: its
    first track ends 10.85 off the centre on the side it turns to, and its next
    track starts on the centre, 10.85 back up the way it goes. The trip steps
    from the first end to its foot on the next track, walking past a stub at
    n1 and round the bend after it, rather than back to that track's start;
    so, sampled every unit, the train is never further back along Y's tracks
    than it was, and never off Y's paint (half of 10.5)."""
    graph = _hopton(turn=TURNS[turn])
    drawn = render(graph, labels=False, strokes=line_strokes(WIDE))
    first, turning = drawn.track("Y", "n0", "n1").points, drawn.track("Y", "n1", "n3").points
    back = (first[-1][0] - turning[0][0]) * (turning[1][0] - turning[0][0]) \
        + (first[-1][1] - turning[0][1]) * (turning[1][1] - turning[0][1])
    assert math.dist(first[-1], turning[0]) == pytest.approx(10.85) and back > 0
    if turn != "straight":
        assert math.dist(turning[0], turning[1]) == pytest.approx(2.0)
    tp = animate.build_trip_path(animate.RouteNetwork.build(drawn), ["n0", "n1", "n3"], "Y",
                                 hops=True)
    assert tp.stop_lengths[-1] == pytest.approx(polyline_length(tp.points))
    places = _places(_samples(tp.points), [first, turning])
    assert max(off for _, off in places) <= 10.5 / 2
    assert not _falls(places), _falls(places)[:3]
    # Straight on or past the stub, the foot is the first track's end itself
    # and the train runs on to n3; round the bend it steps square to the run
    # after the stub, to a foot on neither end of it, and runs on from there.
    after = tp.points[len(first):]
    if turn == "bend":
        assert 1 < math.dist(first[-1], after[0]) < 10.85
        assert min(math.dist(after[0], p) for p in turning) > 1 and after[1:] == turning[2:]
    else:
        assert after == [turning[-1]]


def _joining(points, tracks) -> list[float]:
    """Where each sample of a trip laid over ``tracks`` (as ``_ride`` lays it)
    is along the track it is riding or stepping onto: the distance from the
    run's start to its foot on that track. The track is the one whose share of
    the trip's length holds the sample."""
    laid, spans, total = [tracks[0][0]], [], 0.0
    for track in tracks:
        run = animate._ride(laid, track, True)
        spans.append(total + run)
        total += run
    assert laid == points
    starts, base = [], 0.0
    for track in tracks:
        starts.append(base)
        base += polyline_length(track)
    out, k = [], 0
    for at in range(int(total) + 1):
        while k < len(spans) - 1 and spans[k] < at:
            k += 1
        q = point_at(points, at)
        best, walked = None, starts[k]
        for (ax, ay), (bx, by) in zip(tracks[k], tracks[k][1:]):
            dx, dy = bx - ax, by - ay
            length = math.hypot(dx, dy)
            if length:
                f = max(0.0, min(length, ((q[0] - ax) * dx + (q[1] - ay) * dy) / length))
                off = math.hypot(q[0] - ax - dx * f / length, q[1] - ay - dy * f / length)
                if best is None or off < best[1] - 1e-9:
                    best = (walked + f, off)
            walked += length
        out.append(best[0])
    return out


def test_a_styled_los_angeles_never_backs_a_train_up_the_track_it_joins(tmp_path):
    """Los Angeles from its stored layout with lines widened, cased and dashed:
    no trip, sampled every unit, ever moves back along the track it is riding
    or stepping onto. Also printed: how far any sample is from its own tracks,
    how many paths leave their paint (LOOM's reorders the caps cannot bridge,
    engine issue 70's), and where a sample falls back along the track it is
    leaving, which a turn whose first track runs on past the next one's start
    makes whatever the step."""
    from schematic import pipeline
    found = pipeline.stored("la-metro-rail")
    if found is None:
        pytest.skip("needs the stored layout for la-metro-rail")
    lines = {"A": {"width": 1.5, "casing": {"width": 0.25, "color": "#101010"},
                   "dash": "dashed"},
             "E": {"width": 1.25, "dash": "dotted"}, "K": {"width": 0.75}}
    result = pipeline.run("la-metro-rail", layout=found.id, date=DAY, out_dir=tmp_path,
                          lines=lines)
    assert result.render.strokes and result.animation.paths
    net = animate.RouteNetwork.build(result.render)
    half = {label: 3.5 * (result.render.strokes[label].width
                          if label in result.render.strokes else 1)
            for label in result.render.colors}
    farthest, leaving, left, joined = 0.0, 0, [], []
    for path in result.animation.paths:
        label, nodes = path["route"], path["nodes"]
        tracks, cursor = [], nodes[0]
        for a, b in zip(nodes, nodes[1:]):
            for nxt, tp in net.shortest(label, a, b) or net.shortest(animate.ANY, a, b):
                tracks.append(animate._oriented(tp, cursor))
                cursor = nxt
        trip = animate.build_trip_path(net, nodes, label, hops=True)
        assert trip.path_d() == path["d"]
        places = _places(_samples(trip.points), tracks)
        worst = max(off for _, off in places)
        farthest = max(farthest, worst)
        leaving += worst > half[label] + 1e-6
        left += [(label, *f) for f in _falls(places)]
        went = _joining(trip.points, tracks)
        joined += [(label, i, a, b) for i, (a, b) in enumerate(zip(went, went[1:]))
                   if b < a - 1e-6]
    print(f"\nLos Angeles, styled: {len(result.animation.paths)} paths, the farthest sample "
          f"{farthest:.2f} from its tracks, {leaving} paths off their paint somewhere, "
          f"{len(left)} samples falling back along the track they leave, "
          f"{len(joined)} along the track they join")
    assert not joined, joined[:5]


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
