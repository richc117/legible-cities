"""A caller's line order is a preference, not a whitelist.

``line_order`` says which lines stack over which on shared track, and the
same list orders the rows of the page's linear view. It was a whitelist by
accident: ``render`` drew the labels it named and no others, and
``linear.build`` laid out the same, so an order naming two lines of six
drew two lines and quietly lost four -- on the map, in the chips and in the
time chart alike. The protocol has always said the rest follow. They do
now.
"""

import re

import pytest

from schematic import linear
from schematic.linegraph import LineGraph, ordered_labels
from schematic.render import render


def _graph(labels: list[str]) -> LineGraph:
    """A chain of three stations whose single middle edge carries every line."""
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [i * 10.0, 0.0]},
              "properties": {"id": f"n{i}", "station_id": f"S{i}", "station_label": f"Stop {i}"}}
             for i in range(3)]
    for i in range(2):
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [[i * 10.0, 0.0], [(i + 1) * 10.0, 0.0]]},
                      "properties": {"from": f"n{i}", "to": f"n{i + 1}",
                                     "lines": [{"id": label, "label": label} for label in labels]}})
    return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})


def drawn(svg: str) -> list[str]:
    """The lines the SVG carries, in the order they are painted."""
    return re.findall(r'<g class="line" data-line="([^"]+)"', svg)


# ------------------------------------------------------------- the helper

@pytest.mark.parametrize("order, expected", [
    (None, ["A", "B", "C"]),
    ([], ["A", "B", "C"]),
    (["C"], ["C", "A", "B"]),
    (["C", "A"], ["C", "A", "B"]),
    (["C", "B", "A"], ["C", "B", "A"]),
    # A line the graph does not carry is ignored, as an override for one is.
    (["Z", "C"], ["C", "A", "B"]),
    # Named twice is drawn once, and in the first place it was named.
    (["C", "C", "A"], ["C", "A", "B"]),
])
def test_the_order_names_what_it_can_and_the_rest_follow(order, expected):
    assert ordered_labels(order, ["A", "B", "C"]) == expected


# ------------------------------------------------------------- the drawing

def test_a_partial_order_draws_every_line():
    graph = _graph(["A", "B", "C", "D"])
    svg = render(graph, labels=False, line_order=["D", "B"]).svg
    # The two named first, so they stack over the rest; the others follow in
    # the order they are drawn in without an order at all.
    assert drawn(svg) == ["D", "B", "A", "C"]


def test_no_order_draws_them_alphabetically_as_before():
    graph = _graph(["C", "A", "B"])
    assert drawn(render(graph, labels=False).svg) == ["A", "B", "C"]


def test_an_order_of_every_line_is_obeyed_exactly():
    graph = _graph(["A", "B", "C"])
    assert drawn(render(graph, labels=False, line_order=["C", "A", "B"]).svg) == ["C", "A", "B"]


# ------------------------------------------------------------- the page

def test_the_linear_layout_keeps_the_lines_the_order_leaves_out():
    graph = _graph(["A", "B", "C", "D"])
    layout = linear.build(graph, order=["D", "B"])
    assert [line.label for line in layout.lines] == ["D", "B", "A", "C"]


def test_the_linear_layout_without_an_order_is_unchanged():
    graph = _graph(["C", "A", "B"])
    assert [line.label for line in linear.build(graph).lines] == ["A", "B", "C"]
