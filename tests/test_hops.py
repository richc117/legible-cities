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
import hashlib
import json
import math
from dataclasses import replace
from pathlib import Path

import pytest
from test_animate import TURNS, _hopton, _samples
from test_lines import KEY as STAND_KEY
from test_lines import stand  # noqa: F401  (the fixture: the store and the feed stood in)

from schematic import animate, feeds, pipeline
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


def _pieces(points, tracks) -> list[tuple[str | None, float]]:
    """What each piece of a path laid over ``tracks`` is, and how long:
    ``"ride"`` where it lies on one segment of a track and runs the segment's
    way; ``"step"`` where it goes at a node from the end of one track to a
    point of the next within its first hop of arc, no longer than that hop
    and no further back along the next track than the train already is, the
    point of that first hop of arc nearest where the step starts; ``None``
    where it is neither, and runs back along a track or leaves them."""
    hop = [0.0] + [math.dist(a[-1], b[0]) for a, b in zip(tracks, tracks[1:])]
    k, out = 0, []
    for a, b in zip(points, points[1:]):
        riding = next((j for j in range(k, min(k + 3, len(tracks))) if _rides(a, b, tracks[j])),
                      None)
        if riding is not None:
            k = riding
            out.append(("ride", math.dist(a, b)))
            continue
        for j in range(k + 1, min(k + 3, len(tracks))):
            at = _first(b, tracks[j])
            if math.dist(a, tracks[j - 1][-1]) <= ON and at is not None \
                    and math.dist(a, b) <= hop[j] + ON and at <= hop[j] + ON \
                    and at >= _nearest(a, tracks[j], hop[j]) - ON:
                k = j
                out.append(("step", math.dist(a, b)))
                break
        else:
            out.append((None, math.dist(a, b)))
    return out


def _backs_up(tp: animate.TripPath, tracks) -> list[tuple]:
    """Whatever would back a train up on a trip laid over ``tracks``.

    The page places a train by distance along its path, so its stops must
    not go back and the last must be the path's length. And every piece of
    the path must ride a track its way or step at a node (``_pieces``): a
    step is no longer than the hop there, so the path never doubles back by
    more than it, and lands no further back along the next track than the
    train already is, so the train never moves back along the track it
    joins."""
    stops = tp.stop_lengths
    out: list[tuple] = [("stop", i, a, b) for i, (a, b) in enumerate(zip(stops, stops[1:]))
                        if b < a]
    if stops[-1] != pytest.approx(polyline_length(tp.points)):
        out.append(("length", stops[-1], polyline_length(tp.points)))
    return out + [("piece", i, length) for i, (kind, length)
                  in enumerate(_pieces(tp.points, tracks)) if kind is None]


def _offs(points, tracks) -> list[float]:
    """How far each of a path's samples, a unit apart, is from the nearest
    of ``tracks``; a track whose box is no nearer than the best so far is
    passed over, and the track the last sample was nearest is tried first."""
    boxes = [(min(x for x, _ in t), min(y for _, y in t), max(x for x, _ in t),
              max(y for _, y in t)) for t in tracks]
    out, last = [], 0
    for q in _samples(points):
        best, near = math.inf, last
        for t in [last, *range(len(tracks))]:
            x0, y0, x1, y1 = boxes[t]
            if math.hypot(max(x0 - q[0], 0.0, q[0] - x1), max(y0 - q[1], 0.0, q[1] - y1)) >= best:
                continue
            off = min(_foot(q, a, b)[1] for a, b in zip(tracks[t], tracks[t][1:]))
            if off < best:
                best, near = off, t
        out.append(best)
        last = near
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


def test_a_plain_map_without_a_twin_keeps_the_hop_too(stand, tmp_path,  # noqa: F811
                                                       monkeypatch):
    """The same chain for a feed drawn without its geographic twin: the map's
    own path keeps the step at n1 all the same, so keeping the hop hangs on
    nothing but the map."""
    monkeypatch.setitem(feeds.FEEDS, STAND_KEY,
                        replace(feeds.FEEDS[STAND_KEY], geographic=False))
    result, _svg, data, _page = stand.run(tmp_path / "flat", date=DAY)
    drawn = result.render
    assert not drawn.strokes and not result.animation.geo
    path = next(p for p in data["paths"] if p["route"] == "A")
    assert "gd" not in path
    net = animate.RouteNetwork.build(drawn)
    tp = animate.build_trip_path(net, path["nodes"], "A", hops=True)
    assert path["d"] == tp.path_d()
    hop = [animate._oriented(drawn.track("A", "n0", "n1"), "n0")[-1],
           animate._oriented(drawn.track("A", "n1", "n2"), "n1")[0]]
    at = tp.points.index(hop[0])
    assert tp.points[at:at + 2] == hop and math.dist(*hop) == pytest.approx(HALF_PITCH)


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


# ------------------------------------------- what the hop moves, and only that

# The hashes the two plain maps' data were pinned at before issue 70: the
# stood-in map's positions file (tests/test_gif_palette.py, ``UNCASED``) and
# Pittsburgh's data apart from its lines (tests/fixtures/colors).
STAND_BEFORE_70 = "d9dda941f83720585520c5e9611dba5afad06c6f39303e853536c354bc6e76dc"
PITTSBURGH_BEFORE_70 = "4faa6aa26ea350dabcd144c35f950c5d90075525a1c125b2c750df2d0547ba27"


def _differences(a, b, at: tuple = ()) -> list[tuple]:
    """Where two JSON values differ, each as the keys and indices down to it."""
    if isinstance(a, dict) and isinstance(b, dict):
        return [at + (key,) for key in sorted(a.keys() ^ b.keys())] + [
            found for key in sorted(a.keys() & b.keys())
            for found in _differences(a[key], b[key], at + (key,))]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [found for i, (x, y) in enumerate(zip(a, b))
                for found in _differences(x, y, at + (i,))]
    return [] if a == b else [at]


def _only_the_paths_move(result: pipeline.Result, written: dict) -> tuple[dict, dict]:
    """The page's data built from what the pipeline drew, once cutting the
    chord and once keeping the hop: the second is what was written, and the
    two differ in the paths' geometry and stops and nowhere else."""
    cut, kept = (json.loads(json.dumps(animate.build(
        result.render, result.graph, result.trips, result.date,
        result.animation.geo or None, hops=hops).to_json())) for hops in (False, True))
    assert kept == written
    moved = _differences(cut, kept)
    assert moved
    assert all(at[0] == "paths" and at[2] in ("d", "gd", "stops", "gstops")
               for at in moved), [at for at in moved if at[0] != "paths"][:5] or moved[:5]
    return cut, kept


def test_the_hop_moves_the_stood_in_maps_paths_and_nothing_else(stand, tmp_path):  # noqa: F811
    """The stood-in plain map: keeping the hop moves its paths' geometry and
    stops and nothing else in the page's data, and the positions file it
    writes moves from the hash pinned before issue 70 to the one pinned
    since, which is the whole of that re-pin."""
    result, _svg, _data, _page = stand.run(tmp_path, date=DAY)
    written = (tmp_path / f"{STAND_KEY}.positions.json").read_bytes()
    cut, kept = _only_the_paths_move(result, json.loads(written))
    digest = lambda data: hashlib.sha256(
        json.dumps(data, separators=(",", ":")).encode()).hexdigest()
    assert (digest(cut), digest(kept)) == (STAND_BEFORE_70, hashlib.sha256(written).hexdigest())


def test_the_hop_moves_pittsburghs_paths_and_nothing_else(tmp_path):
    """Pittsburgh from the stored layout its colour snapshot was taken from:
    keeping the hop moves its paths' geometry and stops and nothing else, and
    the data apart from its lines moves from the hash pinned before issue 70
    to the one pinned since, which is the whole of that re-pin."""
    snapshot = json.loads((Path(__file__).parent / "fixtures" / "colors"
                           / "pittsburgh-t.json").read_text())
    found = pipeline.stored("pittsburgh-t")
    if found is None:
        pytest.skip("needs the stored layout for pittsburgh-t")
    if found.id != snapshot["layout"]:
        pytest.skip("the stored Pittsburgh layout is not the one the snapshot was taken from")
    result = pipeline.run("pittsburgh-t", layout=found.id,
                          date=dt.date.fromisoformat(snapshot["date"]), out_dir=tmp_path)
    written = json.loads((tmp_path / "pittsburgh-t.positions.json").read_text())
    cut, kept = _only_the_paths_move(result, written)
    rest = lambda data: hashlib.sha256(json.dumps(
        {k: v for k, v in data.items() if k != "lines"}, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    assert (rest(cut), rest(kept)) == (PITTSBURGH_BEFORE_70, snapshot["data_without_lines"])
    assert sum(any(at[:2] == ("paths", i) for at in _differences(cut, kept))
               for i in range(len(kept["paths"]))) == 6


# ------------------------------------------------ a stored layout, plain

# The stored Los Angeles layout these tests were measured on, built 11 Sep
# 2026; another is skipped, as tests/test_colors.py skips another Pittsburgh.
LOS_ANGELES = "9dc37088cc48a1547e6410844ff80cb32069cc46434f618ccdee6c4d8fff81e8"


@pytest.fixture(scope="module")
def los_angeles(tmp_path_factory) -> list[tuple[dict, animate.TripPath, animate.TripPath,
                                                list]]:
    """Los Angeles drawn from its stored layout with no stroke chosen: each
    path the page was written with, its trip built again to the unit as the
    page has it, the same trip cutting the chord, and the tracks it rides."""
    found = pipeline.stored("la-metro-rail")
    if found is None:
        pytest.skip("needs the stored layout for la-metro-rail")
    if found.id != LOS_ANGELES:
        pytest.skip("the stored Los Angeles layout is not the one these tests were measured on")
    result = pipeline.run("la-metro-rail", layout=found.id, date=DAY,
                          out_dir=tmp_path_factory.mktemp("la-metro-rail"))
    assert not result.render.strokes
    net = animate.RouteNetwork.build(result.render)
    out = []
    for path in result.animation.paths:
        kept, cut = (animate.build_trip_path(net, path["nodes"], path["route"], hops=hops)
                     for hops in (True, False))
        written = next(tp for tp in (kept, cut) if tp.path_d() == path["d"])
        out.append((path, written, cut, _ridden(net, path["route"], path["nodes"])))
    return out


def test_a_plain_los_angeles_keeps_its_trains_nearer_their_paint(los_angeles):
    """Every trip path the page has, sampled every unit, against the drawn
    tracks it rides: wherever it is off them it is on a step at a node,
    within half the step of a painted end; and fewer paths leave their paint
    than with the chord, none as far. Measured, 13 of the 28 leave it by up
    to 5.10 against 21 by up to 8.10, where LOOM reorders the lines at a node
    and the step is wider than the caps bridge; a connector drawn across it
    would close those, and none is drawn."""
    figures = {}
    for name, trip in (("hop", 1), ("chord", 2)):
        leaving, farthest = 0, 0.0
        for row in los_angeles:
            tp, tracks = row[trip], row[3]
            worst = max(_offs(tp.points, tracks))
            if name == "hop":
                steps = [length for kind, length in _pieces(tp.points, tracks) if kind == "step"]
                assert worst <= max(steps, default=0.0) / 2 + ON, (row[0]["route"], worst, steps)
            farthest = max(farthest, worst)
            leaving += worst > HALF + ON
        figures[name] = leaving, farthest
    print(f"\nLos Angeles, plain: {figures['hop'][0]} of {len(los_angeles)} paths off their "
          f"paint somewhere, the farthest sample {figures['hop'][1]:.2f} from its tracks; "
          f"with the chord {figures['chord'][0]}, {figures['chord'][1]:.2f}")
    assert figures["hop"][0] < figures["chord"][0] and figures["hop"][1] < figures["chord"][1]


def test_no_train_on_a_plain_los_angeles_ever_backs_up(los_angeles):
    """Every trip path the page has: its stops in order and the last its
    length, and every piece of it riding a track its way or stepping at a
    node by no more than the hop there, never back along the track it joins
    (``_backs_up``). The chord breaks it, on 23 of the 28 as measured."""
    found = {(path["route"], i): problems for i, (path, tp, _cut, tracks)
             in enumerate(los_angeles) if (problems := _backs_up(tp, tracks))}
    assert not found, {key: problems[:2] for key, problems in list(found.items())[:4]}
    assert any(_backs_up(cut, tracks) for _path, _tp, cut, tracks in los_angeles)
