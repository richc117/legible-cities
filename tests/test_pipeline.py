"""End-to-end invariants on the LA Metro rail map.

These need the LOOM Docker image and the cached feed, so they skip cleanly on a
machine that has not run the pipeline yet. Run ``python -m schematic`` (or the
notebooks) once to populate ``data/`` and they light up.
"""

import math

import pytest

from schematic import animate, feeds
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph
from schematic.offsets import point_at
from schematic.pipeline import GRAPH_DIR
from schematic.render import octilinearity, render
from schematic.schedule import (busiest_weekday, concurrent_trips, match_stops,
                                trips_on)

KEY = "la-metro-rail"
LINES = set("ABCDEK")

pytestmark = pytest.mark.skipif(
    not (GRAPH_DIR / KEY / "03_octi.json").exists(),
    reason="run the pipeline once to populate data/graphs",
)


@pytest.fixture(scope="module")
def graph_ll():
    return LineGraph.from_geojson(GRAPH_DIR / KEY / "03_octi.json")


@pytest.fixture(scope="module")
def graph(graph_ll):
    return graph_ll.reproject(to_mercator)


@pytest.fixture(scope="module")
def tables():
    return feeds.tables(KEY)


@pytest.fixture(scope="module")
def drawn(graph):
    return render(graph, line_order=sorted(LINES))


def test_all_six_rail_lines_are_present(graph):
    assert set(graph.labels) == LINES


def test_the_schematic_is_octilinear(graph):
    """The whole point of the octi stage.

    Measured on the projected graph and weighted by length: LOOM writes lon/lat
    at six decimals, which leaves metre-scale stubs at station nodes whose
    angles are rounding noise.
    """
    ok, total = octilinearity(graph)
    assert ok / total > 0.99


def test_geographic_input_was_not_already_octilinear(graph_ll):
    """Guards the test above from passing on an unschematised graph."""
    raw = LineGraph.from_geojson(GRAPH_DIR / KEY / "00_gtfs2graph.json").reproject(to_mercator)
    ok, total = octilinearity(raw)
    assert ok / total < 0.5


def test_interchanges_are_single_shared_nodes(graph):
    """Shared stations must be one node carrying several lines, not duplicates.

    Expectations are the real interchanges: 7th St/Metro Center is the four-line
    downtown hub, Union Station gained the A Line with the Regional Connector,
    and Willowbrook is where the C Line crosses the A.
    """
    routes_at = {}
    for e in graph.edges:
        for end in (e.src, e.dst):
            routes_at.setdefault(end, set()).update(ln.label for ln in e.lines)
    labelled = {graph.nodes[n].station_label: v for n, v in routes_at.items()}
    for name, expected in [
        ("7th Street / Metro Center Station - Metro A & E Lines", {"A", "B", "D", "E"}),
        ("Union Station - Metro A-Line", {"A", "B", "D"}),
        ("Willowbrook - Rosa Parks Station - Metro C-Line", {"A", "C"}),
        ("Aviation / Imperial Station", {"C", "K"}),
    ]:
        assert name in labelled, f"{name!r} missing; have {sorted(labelled)[:5]}"
        assert labelled[name] == expected

    # Each of those names must appear exactly once -- a duplicated station would
    # mean topo failed to merge the two platforms.
    names = [graph.nodes[n].station_label for n in graph.nodes]
    assert len(names) == len(set(names))


def test_every_scheduled_stop_resolves_to_one_node(graph_ll, tables):
    m = match_stops(graph_ll, tables)
    assert m.unmatched == []
    assert m.coverage == 1.0


def test_no_two_lines_share_a_drawn_track(drawn):
    """Parallel lines must be offset onto their own tracks, not stacked."""
    seen: dict[tuple, str] = {}
    for (label, src, dst), tp in drawn.tracks.items():
        key = (round(tp.points[0][0], 2), round(tp.points[0][1], 2),
               round(tp.points[-1][0], 2), round(tp.points[-1][1], 2))
        assert key not in seen or seen[key] == label, f"{label} shares a track with {seen[key]}"
        seen[key] = label


def test_every_trip_routes_onto_the_map(graph, graph_ll, tables, drawn):
    m = match_stops(graph_ll, tables)
    date = busiest_weekday(tables, LINES)
    trips = trips_on(tables, date, m, LINES)
    anim = animate.build(drawn, graph, trips, date)
    assert anim.unrouted == []
    assert anim.trips_with_skipped_calls == 0
    assert anim.trips_with_borrowed_track == 0
    assert len(anim.trips) == len(trips)
    # A day of service collapses to a handful of stopping patterns.
    assert len(anim.paths) < len(trips) / 10


def test_trains_are_at_their_station_at_the_scheduled_time(graph_ll, tables, drawn):
    """The animation's core claim.

    A train rides its own line's offset track, so at an arrival it sits one
    track-offset from the station centre -- not zero, but never more than that.
    """
    m = match_stops(graph_ll, tables)
    date = busiest_weekday(tables, LINES)
    trips = trips_on(tables, date, m, LINES)
    net = animate.RouteNetwork.build(drawn)

    pitch = 7.0 * 1.6  # Style.line_width * line_gap
    worst = 0.0
    for trip in trips[::40]:
        nodes = [c.node_id for c in trip.calls]
        tp = animate.build_trip_path(net, nodes, trip.route_label)
        assert tp is not None
        for call, length in zip(trip.calls, tp.stop_lengths):
            here = point_at(tp.points, length)
            worst = max(worst, math.dist(here, drawn.node_xy[call.node_id]))
    assert worst <= pitch * 1.05, f"worst arrival miss {worst:.1f}px exceeds one track pitch"


def test_concurrency_matches_an_independent_recount(graph_ll, tables):
    m = match_stops(graph_ll, tables)
    date = busiest_weekday(tables, LINES)
    trips = trips_on(tables, date, m, LINES)
    for hour in (6, 8, 12, 17, 23):
        sec = hour * 3600
        assert (len(concurrent_trips(trips, sec))
                == sum(1 for t in trips if t.start <= sec <= t.end))


def test_service_day_extends_past_midnight(graph_ll, tables):
    """LA runs trains after 24:00; those must not wrap to the start of the day."""
    m = match_stops(graph_ll, tables)
    date = busiest_weekday(tables, LINES)
    trips = trips_on(tables, date, m, LINES)
    assert max(t.end for t in trips) > 24 * 3600
