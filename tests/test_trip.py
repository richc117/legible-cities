"""What the page's trip router reads beside the drawing (engine issue 49).

The router runs in the page (app ADR-048) and reads its graph off the page's
own drawing: every track is one line between two nodes, and every station has a
dot. ``animate.route_rules`` gives it the two things the drawing cannot say, by
label where LOOM writes them by a line's id for each direction: a turn a line
does not make at a node (``excluded_conn``) and a station it passes without
stopping (``not_serving``). These tests hold that shape and where it is
written: into the page, beside the data, and never into the data or the
positions file. The router itself is driven through the page in test_site.py.
"""

import datetime as dt
import json
import re

from schematic import animate
from schematic.linegraph import LineGraph
from schematic.render import render

DAY = dt.date(2026, 9, 10)


def _graph(props: dict[str, dict], b_label: str = "B") -> LineGraph:
    """n0 - n1 - n2 west to east on A, with B leaving n1 north to n3. Each line
    is two ids on every edge, one a direction, as LOOM writes them, and ``props``
    are LOOM's properties by node."""
    at = {"n0": (0.0, 0.0), "n1": (10.0, 0.0), "n2": (20.0, 0.0), "n3": (10.0, 10.0)}
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": list(xy)},
              "properties": {"id": nid, "station_id": "S" + nid, "station_label": "Stop " + nid,
                             **props.get(nid, {})}}
             for nid, xy in at.items()]
    a = [{"id": "0xa1", "label": "A"}, {"id": "0xa2", "label": "A"}]
    b = [{"id": "0xb1", "label": b_label}, {"id": "0xb2", "label": b_label}]
    for src, dst, lines in (("n0", "n1", a + b), ("n1", "n2", a), ("n1", "n3", b)):
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString", "coordinates": [list(at[src]), list(at[dst])]},
                      "properties": {"from": src, "to": dst, "lines": lines}})
    return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})


# A turns back on itself at n1 from n0 to n2, in both its ids, which is one rule by
# its label; B may not turn from n3 to n0, in one direction only; a line the
# graph does not carry is said nothing of; A passes n2 without stopping.
LOOM = {
    "n1": {"excluded_conn": [
        {"line": "0xa1", "node_from": "n0", "node_to": "n2"},
        {"line": "0xa2", "node_from": "n0", "node_to": "n2"},
        {"line": "0xb1", "node_from": "n3", "node_to": "n0"},
        {"line": "0xgone", "node_from": "n0", "node_to": "n3"}]},
    "n2": {"not_serving": ["0xa1", "0xa2"]},
}


def test_the_rules_are_loom_s_by_label_in_the_direction_loom_gives():
    assert animate.route_rules(_graph(LOOM)) == {
        "excluded": {"n1": [["A", "n0", "n2"], ["B", "n3", "n0"]]},
        "unserved": {"n2": ["A"]},
    }


def test_a_hidden_line_takes_its_rules_with_it_and_a_graph_without_any_has_none():
    assert animate.route_rules(_graph(LOOM).without(["B"])) == {
        "excluded": {"n1": [["A", "n0", "n2"]]}, "unserved": {"n2": ["A"]}}
    assert animate.route_rules(_graph(LOOM).without(["A"])) == {
        "excluded": {"n1": [["B", "n3", "n0"]]}}
    assert animate.route_rules(_graph({})) == {}


def _routing_element(page: str) -> str:
    return re.search(r'<script id="routing" type="application/json">(.*?)</script>', page,
                     re.S).group(1)


def test_the_page_carries_the_rules_beside_its_data_and_the_positions_file_does_not(tmp_path):
    graph = _graph(LOOM, b_label="Red_Line")
    drawn = render(graph)
    animation = animate.build(drawn, graph, [], DAY)
    assert animation.routing == animate.route_rules(graph)
    positions, page = animate.write(animation, drawn.svg, tmp_path, stem="rules")
    html = page.read_text(encoding="utf-8")
    element = _routing_element(html)
    # Read back exactly; every underscore written escaped, so no placeholder the
    # page is written with can match inside a feed's label.
    assert json.loads(element) == animation.routing
    assert "Red_Line" in json.dumps(animation.routing) and "_" not in element
    # The data and the positions file are what they were: no rules in either.
    data = json.loads(positions.read_text(encoding="utf-8"))
    assert data == json.loads(json.dumps(animation.to_json())) and "routing" not in data
    assert set(data) == {"date", "lines", "linear", "paths", "trips"}


def test_a_page_whose_graph_has_no_rules_carries_an_empty_object(tmp_path):
    graph = _graph({})
    drawn = render(graph)
    _, page = animate.write(animate.build(drawn, graph, [], DAY), drawn.svg, tmp_path, stem="none")
    assert _routing_element(page.read_text(encoding="utf-8")) == "{}"
