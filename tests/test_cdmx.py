"""Mexico City: the one network here whose feed is frequency-based and expired.

It is also the network the essay argues from, so it is worth asserting that it
actually resolves into a network rather than twelve unconnected stripes.
"""

from collections import defaultdict, deque

import pytest

from schematic import feeds
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph
from schematic.pipeline import GRAPH_DIR
from schematic.render import octilinearity
from schematic.schedule import (busiest_weekday, frequency_windows, match_stops,
                                service_window, trips_on)

KEY = "cdmx-metro"

pytestmark = pytest.mark.skipif(
    not (GRAPH_DIR / KEY / "03_octi.json").exists(),
    reason="run the pipeline once to populate data/graphs",
)


@pytest.fixture(scope="module")
def graph():
    return LineGraph.from_geojson(GRAPH_DIR / KEY / "03_octi.json")


@pytest.fixture(scope="module")
def tables():
    return feeds.tables(KEY)


def test_twelve_lines(graph):
    assert set(graph.labels) == {"1", "2", "3", "4", "5", "6", "7", "8", "9",
                                 "A", "B", "L12"}


def test_the_agency_filter_kept_suburbano_out(tables):
    """Ferrocarriles Suburbanos also numbers a route_type 1 line "1"; without
    the filter it merges into Metro Linea 1."""
    assert set(tables["routes"]["agency_id"]) == {"METRO"}
    assert len(tables["routes"]) == 12


def test_it_is_one_connected_network(graph):
    """Stations carry a per-line id -- 0200L1-PANTITLAN, 0200L5-PANTITLAN -- so
    the interchanges only exist if topo merged them. If this fails the map is
    twelve stripes, not a network."""
    adj = defaultdict(set)
    for e in graph.edges:
        adj[e.src].add(e.dst)
        adj[e.dst].add(e.src)
    seen, components = set(), 0
    for node in graph.nodes:
        if node in seen:
            continue
        components += 1
        queue = deque([node])
        while queue:
            cur = queue.popleft()
            if cur in seen:
                continue
            seen.add(cur)
            queue.extend(adj[cur] - seen)
    assert components == 1


def test_it_is_octilinear(graph):
    ok, total = octilinearity(graph.reproject(to_mercator))
    assert ok / total > 0.99


def test_the_schedule_is_headway_based(tables):
    """If this stops being true the expansion is dead code for this feed."""
    windows = frequency_windows(tables)
    assert len(windows) == len(tables["trips"]) == 72


def test_the_chosen_date_is_inside_the_expired_window(tables):
    start, end = service_window(tables)
    date = busiest_weekday(tables)
    assert start <= date <= end
    assert date.weekday() < 5


def test_expansion_produces_a_full_day_of_service(graph, tables):
    lines = set(graph.labels)
    date = busiest_weekday(tables, lines)
    trips = trips_on(tables, date, match_stops(graph, tables), lines)
    # 72 templates at two-to-four minute headways across the day.
    assert len(trips) > 5000
    assert all(t.calls for t in trips)
    # Every run is a real span of time, not a template stuck at midnight.
    assert all(t.end > t.start for t in trips)
    assert min(t.start for t in trips) >= 4 * 3600


def test_every_stop_resolves(graph, tables):
    m = match_stops(graph, tables)
    assert m.unmatched == []
