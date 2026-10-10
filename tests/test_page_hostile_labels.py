"""A label or a name that looks like something the page is made of (issue 65).

The page is a template whose placeholders were filled one after another. A label
equal to a placeholder (``__DATA__``) was rewritten when the placeholders after
it were filled, because the SVG was written into the page before the data and
the later fill read the earlier one's text again. Every placeholder is now
filled in one pass that never reads what it has written.
"""

import datetime as dt
import json
import re
from html import escape

from schematic import animate
from schematic.linegraph import LineGraph
from schematic.render import render
from schematic.schedule import Call, Trip

DAY = dt.date(2026, 9, 10)
PAGE_DIR = animate._PAGE_DIR
TOKEN = re.compile(r"__[A-Z][A-Z_]*__")

# Every placeholder the template carries, read off the template, so a token
# added to it is covered the day it is added.
TOKENS = sorted(set(TOKEN.findall(animate._HTML)))


def _graph(lines: list[str], stations: list[str], *, ride: bool = False) -> LineGraph:
    """A chain of stations named ``stations``, every line on every edge, and
    the rules LOOM would write for the first line, so the page's routing
    element carries hostile text as well."""
    feats = []
    for i, name in enumerate(stations):
        props = {"id": f"n{i}", "station_id": f"S{i}", "station_label": name}
        if i == 1:
            props["not_serving"] = [lines[0]]
            props["excluded_conn"] = [{"line": lines[0], "node_from": "n0", "node_to": "n2"}]
        feats.append({"type": "Feature",
                      "geometry": {"type": "Point", "coordinates": [i * 10.0, (i % 2) * 6.0]},
                      "properties": props})
    for i in range(len(stations) - 1):
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [[i * 10.0, (i % 2) * 6.0],
                                                   [(i + 1) * 10.0, ((i + 1) % 2) * 6.0]]},
                      "properties": {"from": f"n{i}", "to": f"n{i + 1}",
                                     "lines": [{"id": label, "label": label,
                                                "color": f"#{(k * 40 + 40) % 256:02x}72bc"}
                                               for k, label in enumerate(lines)]}})
    return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})


def _written(tmp_path, graph: LineGraph, headsign: str = "end", **kwargs):
    """The drawn map, its animation and the text of the page written from them,
    with a trip a line over the first edge, so every line has a chip and a train."""
    drawn = render(graph, title="Hostile")
    first = graph.edges[0]
    seven = 7 * 3600
    trips = [Trip(f"t{i}", line.label, headsign,
                  [Call(first.src, first.src, seven, seven + 20),
                   Call(first.dst, first.dst, seven + 120, seven + 140)])
             for i, line in enumerate(first.lines)]
    animation = animate.build(drawn, graph, trips, DAY)
    _, page = animate.write(animation, drawn.svg, tmp_path, stem="hostile", **kwargs)
    return drawn, animation, page.read_text(encoding="utf-8"), page


def _element(page: str, name: str) -> str:
    found = re.findall(rf'<script id="{name}" type="application/json">(.*?)</script>', page, re.S)
    assert len(found) == 1, f"{len(found)} <script id={name}> elements"
    return found[0]


# ------------------------------------------------------------ issue 65

def test_the_template_names_placeholders_and_a_page_leaves_none_unfilled(tmp_path):
    """The guard under the rest: the tokens this file tries are the template's
    own, there are some, and a page written from an ordinary graph has none left."""
    assert {"__DATA__", "__SVG__", "__ROUTING__", "__PRESENT__", "__TITLE__"} <= set(TOKENS)
    plain = _graph(["A", "B"], ["One", "Two", "Three"])
    _, _, page, _ = _written(tmp_path, plain)
    assert TOKEN.findall(page) == []


def test_a_line_or_station_named_for_a_placeholder_is_written_as_the_text_it_is(tmp_path):
    # The first line is the one whose rules the routing element carries.
    lines = (["__DATA__"] + [t for t in TOKENS if t != "__DATA__"]
             + ["</script>", "__SVG__ __DATA__"])
    stations = (TOKENS + ["</script><b>"])[::-1]
    drawn, animation, page, _ = _written(tmp_path, _graph(lines, stations), headsign="__SVG__")
    data, routing = animation.to_json(), animation.routing
    assert routing["unserved"]["n1"] == ["__DATA__"]

    # The map is written once, whole, and nowhere else.
    assert page.count(drawn.svg) == 1
    # So is the data, and it reads back as what was built.
    assert page.count(animate._json_for_script(data)) == 1
    assert json.loads(_element(page, "data")) == json.loads(json.dumps(data))
    assert json.loads(_element(page, "routing")) == routing
    assert page.count('<script id="data"') == 1 and page.count('<script id="routing"') == 1
    assert animate._PRESENT_JS in page

    # Every label is in the SVG as the line's key and in the data as its colour.
    for label in lines:
        assert f'data-line="{escape(label)}"' in drawn.svg, label
        assert label in json.loads(_element(page, "data"))["lines"], label
    # What is left of the page once the three pieces are taken out is the
    # template: no token that was not filled, none that came from a feed.
    shell = (page.replace(drawn.svg, "").replace(animate._json_for_script(data), "")
                 .replace(_element(page, "routing"), ""))
    assert TOKEN.findall(shell) == []

    # And the feed's markup stayed text: the page's script elements are the
    # template's own, as many as a graph with ordinary names gets.
    _, _, tame, _ = _written(tmp_path / "tame", _graph(["A", "B"], ["One", "Two", "Three"]))
    assert page.count("</script") == tame.count("</script")
    assert page.count("<script") == tame.count("<script")


def test_a_title_a_name_and_the_rest_that_name_a_placeholder_are_text_too(tmp_path):
    graph = _graph(["A", "B"], ["One", "Two", "Three"])
    drawn, animation, page, _ = _written(
        tmp_path, graph, title="__SVG__", name="__DATA__", subtitle="__ROUTING__ __TITLE__",
        back="__NAME__ __SVG__", icons="__DATA__", social='<meta content="__SVG__ __DATA__">')
    assert "<title>__SVG__</title>" in page
    assert "<h1>__DATA__</h1>" in page
    assert '<span class="sub">__ROUTING__ __TITLE__</span>' in page
    assert 'href="__NAME__ __SVG__"' in page
    assert 'href="__DATA__/favicon.svg"' in page
    assert '<meta content="__SVG__ __DATA__">' in page
    assert page.count(drawn.svg) == 1
    assert page.count(animate._json_for_script(animation.to_json())) == 1
    assert json.loads(_element(page, "routing")) == animation.routing


def test_a_title_holding_markup_is_still_escaped_and_the_rest_is_as_it_was(tmp_path):
    graph = _graph(["A", "B"], ["One", "Two", "Three"])
    _, _, page, _ = _written(tmp_path, graph, title="<i>T</i> & co", name="N", subtitle="S")
    assert "<title>&lt;i&gt;T&lt;/i&gt; &amp; co</title>" in page
