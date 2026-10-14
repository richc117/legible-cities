"""A train rides the hop at every node, on every map (issue 70).

The map joins no two tracks at a node: each is its own round-capped path, and
a line's two ends there differ wherever its neighbours change, LOOM reorders
it or it turns. A trip's path keeps the step from the one end to the other,
which the round caps bridge up to the line's width, and counts it; where the
step would back a train up the track it joins, it goes to the last end's foot
on that track instead (``animate._ride``). Until issue 70 only a map with a line's own width,
casing or dash kept the step (issue 55); a plain map's trip cut a chord from
the one end to the next track's second point, which could leave the painted
line. The page's data moves for that; the SVG does not.
"""

import datetime as dt
import math

import pytest
from test_animate import TURNS, _hopton, _places, _samples
from test_lines import stand  # noqa: F401  (the fixture: the store and the feed stood in)

from schematic import animate, pipeline
from schematic.offsets import polyline_length
from schematic.render import Style, render
from schematic.schedule import Call, Trip

DAY = dt.date(2026, 9, 10)
# A plain line's paint either side of its track, and the step a line makes
# where it goes from beside one neighbour to the centre: half a pitch.
HALF = Style().line_width / 2
HALF_PITCH = Style().spacing / 2
ON = 1e-6     # a point this near a track is on it (animate.HOP)


def _ridden(net: animate.RouteNetwork, label: str, nodes: list[str]) -> list[list]:
    """The tracks a trip rides, in order and each oriented the way it goes."""
    tracks, cursor = [], nodes[0]
    for a, b in zip(nodes, nodes[1:]):
        for nxt, tp in net.shortest(label, a, b) or net.shortest(animate.ANY, a, b):
            tracks.append(animate._oriented(tp, cursor))
            cursor = nxt
    return tracks


def _foot(q, a, b, most: float = math.inf) -> tuple[float, float]:
    """How far along the segment from ``a`` to ``b``, and no further than
    ``most``, its point nearest ``q`` is, and how far ``q`` is from that."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if not length:
        return 0.0, math.dist(q, a)
    k = max(0.0, min(length, most, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dy) / length))
    return k, math.hypot(q[0] - a[0] - dx * k / length, q[1] - a[1] - dy * k / length)


def _rides(a, b, track) -> bool:
    """Whether the piece from ``a`` to ``b`` lies on one segment of ``track``
    and runs the segment's way, or goes nowhere on it."""
    return any(_foot(a, s, e)[1] <= ON and _foot(b, s, e)[1] <= ON
               and (math.dist(a, b) <= ON
                    or (b[0] - a[0]) * (e[0] - s[0]) + (b[1] - a[1]) * (e[1] - s[1]) > 0)
               for s, e in zip(track, track[1:]))


def _first(q, track) -> float | None:
    """How far along ``track`` ``q`` first lies on it, or None if it never does."""
    walked = 0.0
    for s, e in zip(track, track[1:]):
        k, off = _foot(q, s, e)
        if off <= ON:
            return walked + k
        walked += math.dist(s, e)
    return None


def _nearest(q, track, reach: float) -> float:
    """How far along ``track`` the point of its first ``reach`` of arc nearest
    ``q`` is, of two as near the later: where a train joining the track from
    ``q`` already is along it."""
    best, walked = (0.0, math.dist(q, track[0])), 0.0
    for s, e in zip(track, track[1:]):
        if walked >= reach:
            break
        k, off = _foot(q, s, e, reach - walked)
        if off < best[1] - ON or (off <= best[1] + ON and walked + k > best[0]):
            best = (walked + k, off)
        walked += math.dist(s, e)
    return best[0]


def _backs_up(tp: animate.TripPath, tracks) -> list[tuple]:
    """Whatever would back a train up on a trip laid over ``tracks``.

    The page places a train by distance along its path, so its stops must
    not go back and the last must be the path's length. And the path must be
    made of pieces that each either ride one segment of a track the segment's
    way, or step at a node from the end of one track to a point of the next
    within its first hop of arc: no longer than that hop, so the path never
    doubles back by more than it, and no further back along the next track
    than the train already is, the point of that first hop of arc nearest
    where the step starts, so the train never moves back along the track it
    joins. A piece that is neither runs back along a track or leaves them."""
    stops = tp.stop_lengths
    out: list[tuple] = [("stop", i, a, b) for i, (a, b) in enumerate(zip(stops, stops[1:]))
                        if b < a]
    if stops[-1] != pytest.approx(polyline_length(tp.points)):
        out.append(("length", stops[-1], polyline_length(tp.points)))
    hop = [0.0] + [math.dist(a[-1], b[0]) for a, b in zip(tracks, tracks[1:])]
    k = 0
    for i, (a, b) in enumerate(zip(tp.points, tp.points[1:])):
        riding = next((j for j in range(k, min(k + 3, len(tracks))) if _rides(a, b, tracks[j])),
                      None)
        if riding is not None:
            k = riding
            continue
        for j in range(k + 1, min(k + 3, len(tracks))):
            at = _first(b, tracks[j])
            if math.dist(a, tracks[j - 1][-1]) <= ON and at is not None \
                    and math.dist(a, b) <= hop[j] + ON and at <= hop[j] + ON \
                    and at >= _nearest(a, tracks[j], hop[j]) - ON:
                k = j
                break
        else:
            out.append(("piece", i, a, b, k))
    return out


# ----------------------------------------------------- a hand-made graph

def test_a_plain_map_keeps_the_hop_where_a_lines_neighbours_change(stand, tmp_path):  # noqa: F811
    """The stood-in chain through ``pipeline.run``, no stroke chosen: A runs
    alone from n0 to n1 and beside B from there, so its two ends at n1 are half
    a pitch apart. The path the page places A's train on steps from the one
    end to the other there and rides on, and its stops count the step; the
    geographic twin's path, which the morph places the same train on by the
    same stop, steps across its own two ends at n1 and has as many stops,
    the last its own length."""
    result, _svg, data, _page = stand.run(tmp_path / "plain", date=DAY)
    drawn, geo = result.render, result.animation.geo
    assert not drawn.strokes and geo
    path = next(p for p in data["paths"] if p["route"] == "A")
    assert path["nodes"] == ["n0", "n1", "n2", "n3"]
    net = animate.RouteNetwork.build(drawn)
    tp = animate.build_trip_path(net, path["nodes"], "A", geo, hops=True)
    assert (path["d"], path["gd"]) == (tp.path_d(), tp.path_d(geo=True))
    assert (path["stops"], path["gstops"]) == ([round(v, 1) for v in tp.stop_lengths],
                                               [round(v, 1) for v in tp.geo_stop_lengths])

    first, second = drawn.track("A", "n0", "n1"), drawn.track("A", "n1", "n2")
    hop = [animate._oriented(first, "n0")[-1], animate._oriented(second, "n1")[0]]
    geo_hop = [animate._geo_oriented(first, "n0", geo)[-1],
               animate._geo_oriented(second, "n1", geo)[0]]
    assert math.dist(*hop) == pytest.approx(HALF_PITCH)
    assert math.dist(*geo_hop) > 1
    for points, ends in ((tp.points, hop), (tp.geo_points, geo_hop)):
        at = points.index(ends[0])
        assert points[at:at + 2] == ends
    assert len(tp.geo_stop_lengths) == len(tp.stop_lengths) == 4
    assert tp.stop_lengths[-1] == pytest.approx(polyline_length(tp.points))
    assert tp.geo_stop_lengths[-1] == pytest.approx(polyline_length(tp.geo_points))


@pytest.mark.parametrize("turn", list(TURNS))
def test_a_train_on_a_plain_map_never_backs_up_where_its_line_turns(turn):
    """Hopton with no stroke, through ``animate.build`` as the pipeline calls
    it: Y runs beside X from n0 and turns south at n1, alone, so its first
    track ends half a pitch off the centre on the side it turns to, half a
    pitch down the way it goes, and its next track starts on the centre. The
    chord to the next track's second point ran back up a stub and round a
    bend, and its stops counted the track and not the chord; with the step
    to its foot nothing backs the train up, and the hop is what is built
    unless asked otherwise."""
    graph = _hopton(turn=TURNS[turn])
    drawn = render(graph, labels=False)
    nodes = ["n0", "n1", "n3"]
    trip = Trip("y", "Y", "", [Call(n, n, 7 * 3600 + k * 120, 7 * 3600 + k * 120 + 20)
                               for k, n in enumerate(nodes)])
    built = animate.build(drawn, graph, [trip], DAY)
    net = animate.RouteNetwork.build(drawn)
    tp = animate.build_trip_path(net, nodes, "Y")
    assert [p["d"] for p in built.paths] == [tp.path_d()]
    tracks = _ridden(net, "Y", nodes)
    assert math.dist(tracks[0][-1], tracks[1][0]) == pytest.approx(HALF_PITCH)
    assert not _backs_up(tp, tracks), _backs_up(tp, tracks)[:3]
    assert tp.path_d() == animate.build_trip_path(net, nodes, "Y", hops=True).path_d()


# ------------------------------------------------ a stored layout, plain

@pytest.fixture(scope="module")
def los_angeles(tmp_path_factory) -> list[tuple[dict, animate.TripPath, list]]:
    """Los Angeles drawn from its stored layout with no stroke chosen: each
    path the page was written with, the trip built again to the unit (its
    path the one written), and the tracks it rides."""
    found = pipeline.stored("la-metro-rail")
    if found is None:
        pytest.skip("needs the stored layout for la-metro-rail")
    result = pipeline.run("la-metro-rail", layout=found.id, date=DAY,
                          out_dir=tmp_path_factory.mktemp("la-metro-rail"))
    assert not result.render.strokes
    net = animate.RouteNetwork.build(result.render)
    out = []
    for path in result.animation.paths:
        tp = animate.build_trip_path(net, path["nodes"], path["route"])
        assert tp.path_d() == path["d"]
        out.append((path, tp, _ridden(net, path["route"], path["nodes"])))
    return out


def test_a_plain_los_angeles_keeps_most_of_its_trains_on_their_paint(los_angeles):
    """Every trip path, sampled every unit, against the drawn tracks it rides,
    which on Los Angeles are its own line's: with the chord 21 of the 28 left
    their paint somewhere, by up to 8.10 against a half-width of 3.5. With
    the hop 13 still do, by up to 5.1, where LOOM reorders the lines at a
    node and the step is wider than the caps bridge; a connector drawn across
    it would close those, and none is drawn."""
    leaving, farthest = 0, 0.0
    for _path, tp, tracks in los_angeles:
        worst = max(off for _, off in _places(_samples(tp.points), tracks))
        farthest = max(farthest, worst)
        leaving += worst > HALF + 1e-6
    print(f"\nLos Angeles, plain: {leaving} of {len(los_angeles)} paths off their paint "
          f"somewhere, the farthest sample {farthest:.2f} from its tracks")
    assert len(los_angeles) == 28
    assert leaving <= 13 and farthest <= 5.1


def test_no_train_on_a_plain_los_angeles_ever_backs_up(los_angeles):
    """Every trip path of the plain map: its stops in order and the last its
    length, and every piece of it riding a track its way or stepping at a
    node by no more than the hop there, never back along the track it joins
    (``_backs_up``). With the chord, 23 of the 28 paths broke it."""
    found = {(path["route"], i): problems for i, (path, tp, tracks) in enumerate(los_angeles)
             if (problems := _backs_up(tp, tracks))}
    assert not found, {key: problems[:2] for key, problems in list(found.items())[:4]}
