"""A line's display name, and hiding a line (issue 42, its first half).

``map.build`` takes ``lines``, keyed by label, each ``{name?, hidden?}``.
Hiding is done once, on the graph (app ADR-053): the hidden labels come off
the octi line graph the build reads, and the stop matching, the schedule,
the drawing, the thumbnails, the morph and the page see only the lines left.
Nothing stored changes, and ``render.stage`` still describes every line. A
name is text the page writes where it writes a line's label; the label stays
the key everywhere.

Most of these draw hand-made graphs, with the store and the feed stood in,
so they run without ``data/`` or LOOM. The ones over BART's and LA's stored
layouts skip without them.
"""

import datetime as dt
import hashlib
import json
import math
import re
from pathlib import Path

import pytest
from pylsp_jsonrpc.exceptions import JsonRpcInvalidParams
from test_colors import _graph

from schematic import animate, feeds, pipeline, render as render_module, serve, thumbnail
from schematic.animate import _HTML
from schematic.linegraph import Edge, LineGraph, Node
from schematic.render import Style, render
from schematic.schedule import Call, StopMatch, Trip

KEY = "pittsburgh-t"          # any registered feed: its name and geographic flag are read
LAYOUT = "4e" * 32
DAY = dt.date(2026, 9, 10)
A_COLOUR, B_COLOUR = "#0072bc", "#ff6319"   # B's is used by nothing else

# n0 -A- n1 -B,A- n2 -B,A- n3 -B- n4: A and B share the middle two edges, A
# alone starts the chain and B alone runs on to the last station, so n4 is
# the one station only B serves.
LINES = [[("A", A_COLOUR)], [("B", B_COLOUR), ("A", A_COLOUR)],
         [("B", B_COLOUR), ("A", A_COLOUR)], [("B", B_COLOUR)]]
SCHEMATIC = [(0.0, 0.0), (10.0, 0.0), (20.0, 5.0), (30.0, 5.0), (40.0, 10.0)]
GEOGRAPHIC = [(0.0, 1.0), (11.0, 0.0), (19.0, 6.0), (31.0, 4.0), (40.0, 12.0)]


def _collection(points: list[tuple[float, float]]) -> dict:
    """The chain above in the shape of ``test_colors._graph``, at the given
    coordinates. The edges are written last to first and B is stacked before
    A, so an order rebuilt by sorting shows."""
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": list(xy)},
              "properties": {"id": f"n{i}", "station_id": f"S{i}", "station_label": f"Stop {i}"}}
             for i, xy in enumerate(points)]
    for i in reversed(range(len(LINES))):
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [list(points[i]), list(points[i + 1])]},
                      "properties": {"from": f"n{i}", "to": f"n{i + 1}",
                                     "lines": [{"id": label, "label": label, "color": colour}
                                               for label, colour in LINES[i]]}})
    return {"type": "FeatureCollection", "features": feats}


def chain() -> LineGraph:
    """The chain, a station off it that never had an edge, and two more
    joined by an edge that never carried a line."""
    graph = LineGraph.from_geojson(_collection(SCHEMATIC))
    graph.nodes["n9"] = Node(id="n9", coord=(50.0, 0.0), station_id="S9", station_label="Stop 9")
    graph.nodes["n7"] = Node(id="n7", coord=(50.0, 10.0), station_id="S7", station_label="Stop 7")
    graph.nodes["n8"] = Node(id="n8", coord=(60.0, 10.0), station_id="S8", station_label="Stop 8")
    graph.edges.append(Edge(src="n7", dst="n8", geometry=[(50.0, 10.0), (60.0, 10.0)], lines=[]))
    return graph


def dumped(graph: LineGraph) -> str:
    return json.dumps(graph.to_geojson())


def edge(graph: LineGraph, src: str, dst: str):
    return next(e for e in graph.edges if (e.src, e.dst) == (src, dst))


# ------------------------------------------------------------ LineGraph.without

@pytest.mark.parametrize("labels", [(), {"not-a-label"}], ids=["nothing", "unknown"])
def test_without_nothing_it_carries_is_the_graph_itself(labels):
    graph = chain()
    copy = graph.without(labels)
    assert copy is not graph and copy.edges is not graph.edges
    # The edge that never carried a line, and the two stations only it
    # holds, are not the hiding's to take.
    assert edge(copy, "n7", "n8").lines == [] and {"n7", "n8"} <= set(copy.nodes)
    assert dumped(copy) == dumped(graph)


def test_a_station_only_the_hidden_line_served_goes_and_the_rest_stay():
    graph = chain()
    seen = graph.without({"B"})
    # n4 had an edge and lost it; n9 never had one, and n7 and n8 have one
    # that never carried a line, so none of them is B's to take.
    assert list(seen.nodes) == ["n0", "n1", "n2", "n3", "n9", "n7", "n8"]
    assert [(e.src, e.dst) for e in seen.edges] == \
        [("n2", "n3"), ("n1", "n2"), ("n0", "n1"), ("n7", "n8")]


def test_an_edge_the_lines_shared_carries_the_one_left_in_its_place():
    graph = chain()
    seen = graph.without({"B"})
    assert edge(seen, "n1", "n2").line_labels == ["A"]
    assert edge(seen, "n2", "n3").line_labels == ["A"]
    # And the graph it was asked of is as it was.
    assert edge(graph, "n1", "n2").line_labels == ["B", "A"]
    assert dumped(graph) == dumped(chain())


def test_the_pitch_is_recomputed_over_the_lines_that_remain():
    """Two lines on every edge of a straight chain, one hidden: the other is
    drawn on the edge's centreline, where with both it is half a spacing off."""
    graph = _graph([[("A", None), ("B", None)], [("A", None), ("B", None)]])
    style = Style()
    both = render(graph, style=style, labels=False)
    one = render(graph.without({"B"}), style=style, labels=False)
    for e in graph.edges:
        centre = [one.projection(c) for c in e.geometry]
        assert [both.projection(c) for c in e.geometry] == centre
        alone = one.track("A", e.src, e.dst).points
        shared = both.track("A", e.src, e.dst).points
        assert [math.dist(p, c) for p, c in zip(alone, centre)] == pytest.approx([0.0, 0.0])
        assert [math.dist(p, c) for p, c in zip(shared, centre)] == \
            pytest.approx([style.spacing / 2] * 2)


# ------------------------------------------------- pipeline.run, store stood in

class Stand:
    """The store and the feed, stood in: a stored layout over the chain in
    ``tmp_path``, the schedule's three readers as spies that keep what they
    were given, and ``lay_out`` refused."""

    def __init__(self, tmp_path: Path, monkeypatch) -> None:
        self.folder = tmp_path / "layout"
        self.folder.mkdir()
        loom_graph = json.dumps(_collection(GEOGRAPHIC))
        for stage, name in pipeline.STAGE_FILES.items():
            text = json.dumps(_collection(SCHEMATIC)) if stage == "octi" else loom_graph
            (self.folder / name).write_text(text, encoding="utf-8")
        self.seen: dict = {}
        layout = pipeline.Layout(key=KEY, id=LAYOUT, dir=self.folder, meta={})

        def lay_out(*_args, **_kwargs):
            raise AssertionError("a map from a stored layout lays nothing out")

        def match_stops(graph, tables):
            self.seen["matched"] = graph.labels
            return StopMatch(stop_to_node={f"s{n.id[1:]}": n.id for n in graph.stations},
                             unmatched=[])

        def busiest_weekday(tables, lines=None, *, anchor):
            self.seen["day over"] = set(lines)
            return DAY

        def trips_on(tables, date, match, lines=None):
            # As the real one does: the trips of the lines it is given only.
            self.seen["trips of"] = set(lines)
            runs = {"A": ["n0", "n1", "n2", "n3"], "B": ["n1", "n2", "n3", "n4"]}
            return [Trip(trip_id=f"{label}-{k}", route_label=label, headsign="",
                         calls=[Call(stop_id=f"s{node[1:]}", node_id=node,
                                     arrival=7 * 3600 + k * 600 + i * 120,
                                     departure=7 * 3600 + k * 600 + i * 120 + 30)
                                for i, node in enumerate(nodes)])
                    for label, nodes in runs.items() if label in lines for k in range(3)]

        monkeypatch.setattr(pipeline, "read_layout",
                            lambda key, found: layout if (key, found) == (KEY, LAYOUT) else None)
        monkeypatch.setattr(pipeline, "lay_out", lay_out)
        monkeypatch.setattr(feeds, "tables", lambda *_args, **_kwargs: {"stood": "in"})
        monkeypatch.setattr(pipeline, "match_stops", match_stops)
        monkeypatch.setattr(pipeline, "busiest_weekday", busiest_weekday)
        monkeypatch.setattr(pipeline, "trips_on", trips_on)

    def hashes(self) -> dict[str, str]:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.folder.iterdir())}

    def run(self, out: Path, **kwargs) -> tuple[pipeline.Result, str, dict, str]:
        result = pipeline.run(KEY, layout=LAYOUT, out_dir=out, **kwargs)
        page = (out / f"{KEY}.html").read_text(encoding="utf-8")
        data = json.loads((out / f"{KEY}.positions.json").read_text(encoding="utf-8"))
        return result, (out / f"{KEY}.svg").read_text(encoding="utf-8"), data, page


@pytest.fixture
def stand(tmp_path, monkeypatch) -> Stand:
    return Stand(tmp_path, monkeypatch)


def circle(node: str) -> str:
    return rf'<circle [^>]*data-node="{node}"'


def radius(svg: str, node: str) -> float:
    """The radius a station is drawn with."""
    found = re.search(rf'<circle [^>]*\sr="([0-9.]+)"[^>]*data-node="{node}"', svg)
    assert found, node
    return float(found.group(1))


def test_a_hidden_line_is_taken_off_before_anything_reads_the_graph(stand, tmp_path):
    before = stand.hashes()
    whole = stand.run(tmp_path / "whole")[1]
    result, svg, data, page = stand.run(tmp_path / "out", lines={"B": {"hidden": True}})

    # The map: no group for B, no station only B served, and every station A
    # serves drawn, the ones it shared with B included.
    assert '<g class="line" data-line="A"' in svg
    assert '<g class="line" data-line="B"' not in svg
    assert not re.search(circle("n4"), svg) and 'data-node="n4"' not in svg
    for node in ("n0", "n1", "n2", "n3"):
        assert re.search(circle(node), svg), node
    # A station that was an interchange only because of B is a plain one now.
    style = Style()
    for node in ("n1", "n2", "n3"):
        assert radius(whole, node) == pytest.approx(style.interchange_radius), node
        assert radius(svg, node) == pytest.approx(style.station_radius), node
    assert radius(whole, "n0") == radius(svg, "n0") == pytest.approx(style.station_radius)

    # The page: no colour, no row (so no band) and no trip, so no chip, since
    # the page makes its chips from the trips.
    assert set(data["lines"]) == {"A"}
    assert [line["label"] for line in data["linear"]["lines"]] == ["A"]
    assert "n4" not in data["linear"]["names"]
    assert {trip["r"] for trip in data["trips"]} == {"A"}
    assert animate._json_for_script(data) in page

    # The schedule: the stops matched over the drawn lines, the trips of the
    # drawn lines alone, and the day over every line of the layout.
    # The schedule is read once over every line (the day, and the minutes
    # render.stage describes, are the whole layout's); the drawn map then
    # keeps the drawn lines' trips and the stations that remain.
    assert stand.seen["matched"] == ["A", "B"]
    assert stand.seen["trips of"] == {"A", "B"}
    assert stand.seen["day over"] == {"A", "B"}
    assert "n4" not in set(result.match.stop_to_node.values())
    assert {trip.route_label for trip in result.trips} == {"A"}
    assert result.date == DAY

    # The thumbnails map.build draws from the result's graph.
    for theme, picture in thumbnail.draw(result.graph).items():
        assert A_COLOUR in picture and B_COLOUR not in picture, theme
        assert picture.count("<circle") == 4, theme

    # Nothing stored moved.
    assert result.layout == LAYOUT
    assert stand.hashes() == before


def test_hiding_every_line_is_refused_with_its_own_sentence(stand, tmp_path):
    with pytest.raises(ValueError, match="every line on this map is hidden; show at least "
                                         "one line") as caught:
        stand.run(tmp_path / "out", date=DAY,
                  lines={"A": {"hidden": True}, "B": {"hidden": True}, "Z": {"hidden": True}})
    assert "gtfs2graph" not in str(caught.value)
    # What a client is told: the sentence itself, as a feed error.
    error = serve.classify(caught.value)
    assert error.data["kind"] == "feed" and error.data["hint"] == str(caught.value)
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("lines", [
    {}, {"Z": {"hidden": True, "name": "Zed"}}, {"A": {}, "B": {"hidden": False}},
], ids=["empty", "unknown-label", "nothing-chosen"])
def test_choosing_nothing_draws_what_no_choice_draws_byte_for_byte(stand, tmp_path, lines):
    _, svg, _, page = stand.run(tmp_path / "plain", date=DAY)
    plain = (tmp_path / "plain" / f"{KEY}.positions.json").read_bytes()
    for name, kwargs in (("none", {"lines": None}), ("chosen", {"lines": lines})):
        _, chosen_svg, _, chosen_page = stand.run(tmp_path / name, date=DAY, **kwargs)
        assert chosen_svg == svg and chosen_page == page, name
        assert (tmp_path / name / f"{KEY}.positions.json").read_bytes() == plain, name


# ----------------------------------------------------------------------- names

def test_a_page_without_names_carries_no_names_key():
    graph = _graph([[("A", "0072bc"), ("B", None)]])
    r = render(graph, labels=False)
    assert "names" not in animate.build(r, graph, [], DAY).to_json()
    assert animate.build(r, graph, [], DAY, names={"A": "Airport Line"}).to_json()["names"] == \
        {"A": "Airport Line"}


def test_a_name_reaches_the_page_for_a_drawn_line_only(stand, tmp_path):
    result, svg, data, page = stand.run(
        tmp_path / "out", date=DAY,
        lines={"A": {"name": "Airport Line"}, "B": {"hidden": True, "name": "Bay Line"},
               "Z": {"name": "Zed"}})
    assert data["names"] == result.animation.names == {"A": "Airport Line"}
    element = page[page.index('<script id="data"'):]
    assert '"names":{"A":"Airport Line"}' in element[:element.index("</script>")]
    assert "Bay Line" not in page and "Zed" not in page
    # The label stays the key: the SVG writes no line text, and the trips
    # and colours are keyed by label.
    assert "Airport Line" not in svg and '<g class="line" data-line="A"' in svg
    assert set(data["lines"]) == {trip["r"] for trip in data["trips"]} == {"A"}


def test_the_page_writes_a_lines_name_where_it_writes_its_label():
    """Text over the template: the row (whose name the time chart reuses as
    its band's label), the train's title and the chip each write
    ``display(...)``, the A-Z sort compares what the row shows, and the
    helper is defined once."""
    assert _HTML.count("const display = ") == 1
    assert "name.textContent = display(line.label);" in _HTML
    assert 'title.textContent = display(trip.r) + (trip.h ? " to " + trip.h : "");' in _HTML
    assert "chip.append(dot, display(r));" in _HTML
    assert "display(a).localeCompare(display(b), undefined, { numeric: true })" in _HTML
    # The label stays the key the page matches on.
    assert '\'#lines g.line[data-line="\' + r + \'"]\'' in _HTML


def test_a_name_with_a_line_break_anywhere_is_refused():
    """The pattern is anchored at both ends, and Python's ``$`` also matches
    before a final newline, so the server matches the whole name."""
    for name in ("Airport\n", "Airport\r", "Air\u2028port", "Airport\u2029"):
        with pytest.raises(JsonRpcInvalidParams, match=r"lines\['A'\]\.name must be"):
            serve._lines({"lines": {"A": {"name": name}}})
    assert serve._lines({"lines": {"A": {"name": "x" * 40}}}) == {"A": {"name": "x" * 40}}


# ------------------------------------------------------ over the stored layouts

def _newest(key: str) -> pipeline.Layout:
    found = pipeline.stored_layouts(key)
    if not found:
        pytest.skip(f"needs a stored layout for {key}")
    return found[0]


def _sha(paths: dict[str, Path]) -> dict[str, str]:
    return {stage: hashlib.sha256(path.read_bytes()).hexdigest() for stage, path in paths.items()}


def _paired(result: pipeline.Result) -> set[tuple[str, str, str]]:
    return {key for key, track in result.render.tracks.items()
            if track.element_id in result.animation.geo.tracks}


def test_bart_with_grey_hidden(tmp_path):
    """The two stations only Grey serves, as the stored graph labels them,
    are gone; everything else is the map it was, and the store is as it was."""
    layout = _newest("bart")
    before = _sha(layout.paths)
    stage_before = render_module.stage("bart", "octi", layout=layout.id)
    whole = pipeline.run("bart", layout=layout.id, out_dir=tmp_path / "whole")
    seen = pipeline.run("bart", layout=layout.id, out_dir=tmp_path / "hidden",
                        lines={"Grey": {"hidden": True}})

    gone = {n.id for n in whole.graph.stations
            if n.station_label in ("Coliseum - OAC", "Oakland International Airport Station")}
    assert len(gone) == 2 and {n.id for n in whole.graph.stations} - {
        n.id for n in seen.graph.stations} == gone
    svg = (tmp_path / "hidden" / "bart.svg").read_text(encoding="utf-8")
    data = json.loads((tmp_path / "hidden" / "bart.positions.json").read_text(encoding="utf-8"))
    for node in gone:
        sid = whole.graph.nodes[node].station_id
        assert f'data-station-id="{sid}"' not in svg and f'data-node="{node}"' not in svg
        assert node not in data["linear"]["names"]
    assert '<g class="line" data-line="Grey"' not in svg
    assert "Grey" not in data["lines"] and "Grey" in whole.animation.lines
    assert "Grey" in {trip["r"] for trip in whole.animation.trips}
    assert "Grey" not in {trip["r"] for trip in data["trips"]}
    assert seen.date == whole.date

    grey = whole.render.colors["Grey"]
    plain, hidden = thumbnail.draw(whole.graph), thumbnail.draw(seen.graph)
    for theme in hidden:
        assert hidden[theme].count("<circle") == plain[theme].count("<circle") - 2
        assert f'<g stroke="{grey}"' in plain[theme] and f'<g stroke="{grey}"' not in hidden[theme]

    assert seen.layout == whole.layout == layout.id
    assert _sha(layout.paths) == before
    assert render_module.stage("bart", "octi", layout=layout.id) == stage_before
    # The morph pairs what it paired before, but Grey's tracks.
    assert _paired(seen) == {key for key in _paired(whole) if key[0] != "Grey"}
    print(f"bart: {len(_paired(seen))} of {len(seen.render.tracks)} tracks pair with Grey "
          f"hidden, {len(_paired(whole))} of {len(whole.render.tracks)} whole")


def test_los_angeles_with_k_hidden_pairs_every_drawn_track(tmp_path):
    layout = _newest("la-metro-rail")
    result = pipeline.run("la-metro-rail", layout=layout.id, date=dt.date(2026, 9, 11),
                          out_dir=tmp_path, lines={"K": {"hidden": True}})
    assert "K" not in result.render.colors
    assert len(result.animation.geo.tracks) == len(result.render.tracks)
    assert len(result.animation.geo.nodes) == len(result.graph.stations)
