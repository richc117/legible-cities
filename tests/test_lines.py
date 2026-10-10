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
    {"A": {"width": 1, "casing": {"width": 0, "color": "#101010"}, "dash": "solid"},
     "Z": {"width": 1.5, "dash": "dotted"}},
], ids=["empty", "unknown-label", "nothing-chosen", "stroke-defaults"])
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


# ------------------------------------------- width, casing and dash (issue 55)
#
# A line's own stroke: ``width`` (0.75 to 1.5) and a ``casing`` either side
# (``{width, color}``, 0 to 1 a side), both multiples of ``line_width``, and a
# ``dash``. A line's slot is ``(w + 2c) * line_width`` and the lines beside it
# on its own edges move out to make it (``offsets.slot_offsets``).

# n0 -A,C- n1 -A,B- n2 -A,B,C- n3 -B,C- n4 -A,C- n5: B runs on the middle three
# edges, and the first and last carry two lines B never shares, so a width that
# leaked onto every edge would move them.
STROKED = [[("A", "0072bc"), ("C", "00933c")], [("A", "0072bc"), ("B", "ff6319")],
           [("A", "0072bc"), ("B", "ff6319"), ("C", "00933c")],
           [("B", "ff6319"), ("C", "00933c")], [("A", "0072bc"), ("C", "00933c")]]
CASED = {"width": 1.5, "casing": {"width": 0.5, "color": "#101010"}, "dash": "dashed"}


def _paths(svg: str) -> dict[str, str]:
    """Every track's path data, by its element id."""
    return dict(re.findall(r'<path id="([^"]+)" data-src="[^"]*" data-dst="[^"]*" d="([^"]+)"',
                           svg))


def _group(svg: str, label: str) -> tuple[str, str]:
    """A line's group: its opening tag and what is inside it."""
    found = re.search(rf'(<g class="line" data-line="{label}"[^>]*>)(.*?)</g>', svg, re.S)
    assert found, label
    return found.group(1), found.group(2)


def _offset(track: list[tuple[float, float]], centre: list[tuple[float, float]]) -> float:
    """How far a straight track lies from its edge's centreline, signed to the
    left of travel."""
    (x0, y0), (x1, y1) = centre[0], centre[-1]
    length = math.hypot(x1 - x0, y1 - y0)
    px, py = track[0]
    return ((x1 - x0) * (py - y0) - (y1 - y0) * (px - x0)) / length


@pytest.mark.parametrize("chosen", [{"width": 1.5}, {"casing": {"width": 0.5, "color": "#101010"}},
                                    CASED], ids=["width", "casing", "all-three"])
def test_a_width_or_casing_moves_tracks_only_on_the_edges_its_line_runs_on(chosen):
    """B widened or cased: on the two edges B never runs on every path is the
    one the plain map draws, byte for byte; on B's own edges B keeps its centre
    and the lines beside it move out by half its extra room."""
    graph = _graph(STROKED)
    style = Style()
    strokes = render_module.line_strokes({"B": chosen})
    plain = render(graph, style=style, labels=False)
    wide = render(graph, style=style, labels=False, strokes=strokes)
    before, after = _paths(plain.svg), _paths(wide.svg)
    assert set(before) == set(after)
    extra = (strokes["B"].slot - 1) * style.line_width
    moved = 0
    for e in graph.edges:
        centre = [plain.projection(c) for c in e.geometry]
        for line in e.lines:
            key = (line.label, e.src, e.dst)
            eid = plain.tracks[key].element_id
            if "B" not in e.line_labels or line.label == "B":
                # Off B's edges, and B itself, nothing moved.
                assert after[eid] == before[eid], (key, chosen)
                continue
            shift = _offset(wide.tracks[key].points, centre) - _offset(plain.tracks[key].points,
                                                                       centre)
            i, j = e.line_labels.index(line.label), e.line_labels.index("B")
            assert shift == pytest.approx(extra / 2 * (1 if i > j else -1), abs=1e-9), key
            moved += 1
    assert moved == 4


def test_a_line_is_drawn_at_its_width_and_dash_over_one_casing_per_track():
    """B at 1.5 with a casing of 0.5 a side, dashed: its group strokes 10.5 and
    dashes 10.5 on and 21 off (2t on and t off with the round caps); before its
    tracks it holds one use of each of them, in the casing's colour at the
    slot's width, 17.5, and undashed. No other group has a use or a dash."""
    graph = _graph(STROKED)
    r = render(graph, style=Style(), labels=False,
               strokes=render_module.line_strokes({"B": CASED}))
    tag, body = _group(r.svg, "B")
    assert 'stroke-width="10.50"' in tag and 'stroke-dasharray="10.50 21.00"' in tag
    tracks = [tp.element_id for tp in r.tracks.values() if tp.label == "B"]
    assert len(tracks) == 3
    children = re.findall(r"<(use|path)\b([^>]*)/>", body)
    assert [kind for kind, _ in children] == ["use"] * 3 + ["path"] * 3
    for (_, use), eid in zip(children, tracks):
        assert use == (f' href="#{eid}" stroke="#101010" stroke-width="17.50" '
                       f'stroke-dasharray="none"')
    assert [re.search(r'id="([^"]+)"', attrs).group(1) for _, attrs in children[3:]] == tracks
    for label in ("A", "C"):
        tag, body = _group(r.svg, label)
        assert tag == f'<g class="line" data-line="{label}" stroke="{r.colors[label]}" ' \
                      f'stroke-width="7.00">'
        assert "<use" not in body
    assert r.svg.count("<use") == 3 and r.svg.count("stroke-dasharray") == 4


@pytest.mark.parametrize("dash, width, wanted", [
    ("dashed", 1, "7.00 14.00"), ("dotted", 1, "0 11.20"), ("dashed", 0.75, "5.25 10.50"),
    ("dotted", 1.5, "0 16.80"), ("solid", 1.5, None)])
def test_a_dash_scales_with_the_lines_own_stroke(dash, width, wanted):
    """Dashed is t on and 2t off, dotted 0 on and 1.6t off, t the line's own
    stroke, and solid writes no dash at all."""
    graph = _graph(STROKED)
    r = render(graph, labels=False,
               strokes=render_module.line_strokes({"A": {"width": width, "dash": dash}}))
    tag, _ = _group(r.svg, "A")
    assert f'stroke-width="{7 * width:.2f}"' in tag
    found = re.search(r'stroke-dasharray="([^"]+)"', tag)
    assert (found.group(1) if found else None) == wanted


def test_choosing_the_defaults_draws_what_choosing_nothing_draws():
    """Width 1, a casing of width 0 and solid are no stroke at all: the map is
    the plain one byte for byte, with labels, and so are both thumbnails."""
    graph = _graph(STROKED)
    defaults = {"width": 1, "casing": {"width": 0, "color": "#101010"}, "dash": "solid"}
    assert render_module.line_strokes({"A": defaults, "B": {}, "C": {"name": "Sea"}}) == {}
    plain = render(graph, title="Plain")
    assert render(graph, title="Plain",
                  strokes=render_module.line_strokes({"A": defaults})).svg == plain.svg
    assert thumbnail.draw(graph, strokes=render_module.line_strokes({"A": defaults})) == \
        thumbnail.draw(graph)


def test_labels_markers_and_the_canvas_keep_clear_of_a_widened_slot():
    """A cased line's room reaches past a plain one's: the drawing's boxes
    grow to hold it, and a tick on it stands TICK of its slot past the slot's
    edge, as a tick on a plain line stands past the line's."""
    graph = _graph([[("A", "0072bc")], [("A", "0072bc")]])
    style = Style(station_shape="tick")
    plain = render(graph, style=style)
    cased = render(graph, style=style, strokes=render_module.line_strokes(
        {"A": {"width": 1.5, "casing": {"width": 1, "color": "#101010"}}}))

    def box(svg: str, attribute: str) -> list[float]:
        return [float(v) for v in re.search(rf'{attribute}="([^"]+)"', svg).group(1).split()]

    assert box(cased.svg, "data-viewbox-nolabels")[3] > box(plain.svg, "data-viewbox-nolabels")[3]

    def reach(svg: str) -> float:
        """How far the tick at n1 reaches across its line (which runs east),
        from the corners its relative steps walk."""
        d = re.search(r'<path d="M [-\d.]+ [-\d.]+ ([^"]*) z" fill="[^"]+" data-node="n1"',
                      svg).group(1)
        y, far = 0.0, 0.0
        for _, dy in re.findall(r"[ml] ([-\d.]+) ([-\d.]+)", d):
            y += float(dy)
            far = max(far, abs(y))
        return far

    # A tick reaches from the line's middle to half its size and TICK more.
    assert reach(plain.svg) == pytest.approx(7 / 2 + 0.66 * 7, abs=0.02)
    assert reach(cased.svg) == pytest.approx(3.5 * 7 / 2 + 0.66 * 3.5 * 7, abs=0.02)


def test_the_thumbnails_draw_a_lines_width_casing_and_dash():
    """At the thumbnail's own line width (2.8), B at 1.5 is 4.2, dashed 4.2 on
    and 8.4 off, over its casing at 2.5 lines, 7; A and C are as they were."""
    graph = _graph(STROKED)
    plain = thumbnail.draw(graph)
    drawn = thumbnail.draw(graph, strokes=render_module.line_strokes({"B": CASED}))
    for theme, picture in drawn.items():
        group = re.search(r'<g stroke="#ff6319"[^>]*>.*?</g>', picture).group(0)
        assert group.startswith('<g stroke="#ff6319" stroke-width="4.2" '
                                'stroke-dasharray="4.2 8.4"><path d="')
        casing, own = re.findall(r"<path ([^>]*)/>", group)
        assert casing.endswith(' stroke="#101010" stroke-width="7" stroke-dasharray="none"')
        assert casing.split(" stroke=")[0] == own
        for colour in ("#0072bc", "#00933c"):
            assert re.search(rf'<g stroke="{colour}" stroke-width="2.8"><path d=', picture)
        assert picture != plain[theme]


# The wire: each field's bounds, and a refusal that names it by its path.
GOOD_STROKES = [{"width": 0.75}, {"width": 1.5}, {"width": 1}, {"dash": "dotted"},
                {"casing": {"width": 0, "color": "#000000"}},
                {"casing": {"width": 1, "color": "#ABCDEF"}},
                {"name": "Bay", "hidden": False, **CASED}]
BAD_STROKES = [
    ({"width": 0.74}, "lines['A'].width must be from 0.75 to 1.5, as a multiple of line_width"),
    ({"width": 1.51}, "lines['A'].width must be from 0.75 to 1.5"),
    ({"width": float("nan")}, "lines['A'].width must be from"),
    ({"width": "1"}, "lines['A'].width must be from"),
    ({"width": True}, "lines['A'].width must be from"),
    ({"width": None}, "lines['A'].width must be from"),
    ({"casing": {"width": -0.01, "color": "#000000"}},
     "lines['A'].casing.width must be from 0 to 1, as a multiple of line_width on each side"),
    ({"casing": {"width": 1.01, "color": "#000000"}}, "lines['A'].casing.width must be from 0"),
    ({"casing": {"width": 0.5, "color": "black"}},
     "lines['A'].casing.color must be a colour written #rrggbb"),
    ({"casing": {"width": 0.5, "color": "#000000\n"}}, "lines['A'].casing.color must be"),
    ({"casing": {"width": 0.5}}, "lines['A'].casing must have both width and color"),
    ({"casing": {"color": "#000000"}}, "lines['A'].casing must have both width and color"),
    ({"casing": {"width": 0.5, "color": "#000000", "dash": "dotted"}},
     "lines['A'].casing must be an object of width and color"),
    ({"casing": 0.5}, "lines['A'].casing must be an object of width and color"),
    ({"dash": "dash-dot"}, "lines['A'].dash must be solid, dashed or dotted"),
    ({"dash": None}, "lines['A'].dash must be solid, dashed or dotted"),
    ({"stroke": 2}, "lines['A'] does not take stroke"),
]


@pytest.mark.parametrize("chosen", GOOD_STROKES)
def test_a_stroke_inside_its_bounds_is_taken_and_the_schema_agrees(chosen):
    from test_serve import check
    assert serve._lines({"lines": {"A": chosen}}) == {"A": chosen}
    check({"key": KEY, "layout": LAYOUT, "date": DAY.isoformat(), "lines": {"A": chosen}},
          "MapBuildParams")


@pytest.mark.parametrize("chosen, sentence", BAD_STROKES)
def test_a_stroke_out_of_bounds_is_refused_naming_its_field_and_the_schema_agrees(
        chosen, sentence):
    from test_serve import invalid
    with pytest.raises(JsonRpcInvalidParams) as caught:
        serve._lines({"lines": {"A": chosen}})
    assert caught.value.data["kind"] == "params"
    assert caught.value.data["hint"].startswith(sentence), caught.value.data["hint"]
    # Two the schema cannot see: JSON has no NaN, and Python's jsonschema reads
    # a pattern with re.search, which takes a trailing newline (issue 57).
    if chosen.get("width") == chosen.get("width") and r"\n" not in json.dumps(chosen):
        assert invalid({"key": KEY, "layout": LAYOUT, "date": DAY.isoformat(),
                        "lines": {"A": chosen}}, "MapBuildParams")


def test_the_bounds_are_one_table_with_the_schemas():
    from test_serve import SCHEMA
    options = SCHEMA["$defs"]["LineOptions"]["properties"]
    casing = SCHEMA["$defs"]["LineCasing"]
    assert (options["width"]["minimum"], options["width"]["maximum"]) == \
        render_module.LINE_WIDTH_RANGE
    assert (casing["properties"]["width"]["minimum"], casing["properties"]["width"]["maximum"]) \
        == render_module.CASING_WIDTH_RANGE
    assert options["dash"]["enum"] == list(render_module.LINE_DASHES)
    assert casing["required"] == ["width", "color"]


def test_a_python_caller_is_refused_a_stroke_the_server_would_refuse():
    for bad in ({"width": 2}, {"dash": "dash-dot"}, {"casing": {"width": 2, "color": "#000000"}},
                {"casing": {"width": 0.5, "color": "black"}}):
        with pytest.raises(ValueError):
            render_module.line_strokes({"A": bad})


def test_the_pipeline_draws_a_drawn_lines_stroke_and_drops_a_hidden_ones(stand, tmp_path):
    """Through ``pipeline.run``: B's stroke reaches its group, and a stroke on
    a hidden line or a label the layout does not carry changes nothing."""
    _, plain, _, _ = stand.run(tmp_path / "plain", date=DAY)
    _, svg, _, _ = stand.run(tmp_path / "out", date=DAY, lines={"B": CASED})
    assert 'stroke-width="10.50" stroke-dasharray="10.50 21.00"' in _group(svg, "B")[0]
    assert svg.count("<use ") == 3
    _, hidden, _, _ = stand.run(tmp_path / "hidden", date=DAY,
                                lines={"B": {"hidden": True, **CASED}, "Z": CASED})
    _, without, _, _ = stand.run(tmp_path / "without", date=DAY, lines={"B": {"hidden": True}})
    assert hidden == without and "<use" not in hidden
    assert plain != svg


def test_map_build_draws_the_strokes_into_the_thumbnails(tmp_path, monkeypatch):
    """The server hands the lines' strokes to the thumbnails it writes beside
    the page, drawn from the graph the pipeline answers, as it hands the
    colours and the order. Stood in as test_serve's thumbnail test is."""
    from types import SimpleNamespace

    from test_serve import NO_LAYOUT, Client, check

    from schematic import config, diagnostics
    monkeypatch.setenv(config.ENV, str(tmp_path))
    graph = _graph(STROKED)
    diag = diagnostics.Diagnostics(
        key="p9", name="Nine", date=DAY, stations=6, junctions=0, edges=5, lines=("A", "B", "C"),
        octilinear=1.0, stops=diagnostics.StopMatching(6, 6, 6, 0, 0, ()), trips_total=0,
        paths=0, unrouted=0, skipped_calls=0, borrowed_track=0, labels_dropped=0,
        peak_concurrent=0)

    def stood_in(key, **kwargs):
        kwargs["out_dir"].mkdir(parents=True)
        return SimpleNamespace(layout=NO_LAYOUT, date=DAY, graph=graph, diagnostics=lambda: diag)

    monkeypatch.setattr(pipeline, "run", stood_in)
    client = Client()
    try:
        response = client.call("map.build", {"key": "la-metro-rail", "layout": NO_LAYOUT,
                                             "date": DAY.isoformat(), "out": "p9",
                                             "lines": {"B": CASED}})
    finally:
        client.endpoint.close()
    assert "result" in response, response
    check(response["result"], "MapBuildResult")
    drawn = thumbnail.draw(graph, strokes=render_module.line_strokes({"B": CASED}))
    for theme in ("dark", "light"):
        written = Path(response["result"]["files"][f"thumb_{theme}"]).read_text(encoding="utf-8")
        assert written == drawn[theme] != thumbnail.draw(graph)[theme]


def test_two_labels_one_edge_and_one_id_apart_get_ids_of_their_own():
    """"A-1" and "A 1" both make t0_A_1, so the second on an edge takes
    t0_A_1_2, and each casing draws its own line's track; a map with no such
    pair keeps the ids it had (the Pittsburgh pin holds that too)."""
    graph = _graph([[("A-1", "0072bc"), ("A 1", "ff6319")], [("A-1", "0072bc")]])
    cased = {"width": 1, "casing": {"width": 0.5, "color": "#101010"}}
    r = render(graph, labels=False, strokes=render_module.line_strokes({"A-1": cased,
                                                                        "A 1": cased}))
    ids = [tp.element_id for tp in r.tracks.values()]
    assert ids == ["t0_A_1", "t0_A_1_2", "t1_A_1"] and len(set(ids)) == len(ids)
    for label in ("A-1", "A 1"):
        _, body = _group(r.svg, label)
        own = re.findall(r'<path id="([^"]+)"', body)
        assert re.findall(r'<use href="#([^"]+)"', body) == own
        assert own == [tp.element_id for tp in r.tracks.values() if tp.label == label]
    assert [tp.element_id for tp in render(_graph(STROKED), labels=False).tracks.values()] == [
        f"t{ei}_{label}" for ei, edge in enumerate(STROKED) for label, _ in edge]
