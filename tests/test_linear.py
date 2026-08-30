"""The linear layout: every line as a row, branches forked below it."""

import pytest

from schematic import feeds, linear
from schematic.linegraph import LineGraph
from schematic.pipeline import GRAPH_DIR

KEY = "la-metro-rail"

pytestmark = pytest.mark.skipif(
    not (GRAPH_DIR / KEY / "03_octi.json").exists(),
    reason="run the pipeline once to populate data/graphs",
)


def built_graphs():
    for key in feeds.FEEDS:
        path = GRAPH_DIR / key / "03_octi.json"
        if path.exists():
            yield key, LineGraph.from_geojson(path)


@pytest.fixture(scope="module")
def la():
    return LineGraph.from_geojson(GRAPH_DIR / KEY / "03_octi.json")


def test_every_station_is_placed_exactly_once():
    """Across every built network, not just the convenient one.

    A station silently missing from the layout is a train with nowhere to be.
    """
    for key, graph in built_graphs():
        layout = linear.build(graph)
        for line in layout.lines:
            _, nodes = linear._adjacency(graph, line.label)
            placed = [n for row in line.rows for n, _ in row.nodes]
            assert sorted(placed) == sorted(nodes), f"{key} {line.label}"
            assert len(placed) == len(set(placed)), f"{key} {line.label} duplicated"


def test_no_two_stations_share_a_column_within_a_row():
    for key, graph in built_graphs():
        for line in linear.build(graph).lines:
            for row in line.rows:
                cols = [c for _, c in row.nodes]
                assert len(cols) == len(set(cols)), f"{key} {line.label} depth {row.depth}"


def test_a_simple_line_is_one_row_in_timetable_order(la):
    """LA's E Line runs end to end with no branches, so it must be a single row
    whose order matches the line's own chain of stations."""
    line = next(l for l in linear.build(la).lines if l.label == "E")
    assert len(line.rows) == 1
    adj, nodes = linear._adjacency(la, "E")
    order = [n for n, _ in line.rows[0].nodes]
    assert len(order) == len(nodes)
    # Consecutive entries must actually be connected on the line.
    for a, b in zip(order, order[1:]):
        assert b in adj[a], f"{a} and {b} are not adjacent on the E Line"


def test_columns_are_consecutive_along_a_spine(la):
    for line in linear.build(la).lines:
        cols = [c for _, c in line.rows[0].nodes]
        assert cols == list(range(len(cols)))


def test_a_branch_starts_past_its_junction(la):
    """LA's A Line ends in the Long Beach loop, which forks off the spine."""
    line = next(l for l in linear.build(la).lines if l.label == "A")
    assert len(line.rows) > 1, "the A Line should have a branch"
    spine_cols = {c for _, c in line.rows[0].nodes}
    for row in line.rows[1:]:
        assert row.depth > 0
        assert row.start_col > 0
        assert row.start_col <= max(spine_cols) + 1


def test_disconnected_lines_get_a_row_each():
    """NYC labels three unconnected shuttles 'S'."""
    path = GRAPH_DIR / "nyc-subway" / "03_octi.json"
    if not path.exists():
        pytest.skip("nyc-subway not built")
    graph = LineGraph.from_geojson(path)
    line = next(l for l in linear.build(graph).lines if l.label == "S")
    assert len(line.rows) >= 3
    placed = [n for row in line.rows for n, _ in row.nodes]
    assert len(placed) == len(set(placed))


def test_payload_shape(la):
    layout = linear.build(la, order=list("ABCDEK"))
    js = layout.to_json()
    assert [l["label"] for l in js["lines"]] == list("ABCDEK")
    assert js["columns"] == max(l.width for l in layout.lines)
    for line in js["lines"]:
        assert line["stations"] == sum(len(r["nodes"]) for r in line["rows"])
