"""render.stage's description: a stage graph in words-ready fields (issue 54).

The app writes a text alternative for the stage it shows from these fields
and computes nothing itself, so what the words need is here: per line its
ends, its stations in order, where it meets other lines and its branches,
and, given the project's service day, how long its commonest trip takes.

Most of it runs on a graph built in code with LOOM-like hex ids, small
enough that its whole description is written out by hand below. The minutes
are read through a scratch home whose one layout holds that graph, with the
day's reading stood in for, so nothing here downloads a feed. BART's
criterion and the cache ``map.build`` fills read the checkout's stored
layouts and skip without them.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter

import pytest
from test_serve import Client, check

from schematic import config, feeds, linear, pipeline, render, schedule
from schematic.describe import describe, station_name
from schematic.linegraph import Edge, Line, LineGraph, Node

KEY = "la-metro-rail"
LAYOUT = "5e" * 32
DAY = dt.date(2026, 9, 11)

# id: (name, x, y). A name of None is a station the feed leaves unnamed; an
# id missing here is a junction LOOM inserted.
STATIONS = {
    "0x100": ("Exchange", 2, 0), "0x101": ("Alder", 0, 0), "0x102": ("Ash", 1, 0),
    "0x301": ("Bay", 3, 0), "0x302": ("Cove", 4, 0), "0x304": ("Dune", 6, 0),
    "0x305": ("Elm", 7, 0), "0x306": ("Mill", 5, 1),
    "0x400": ("Fern", 0, 3), "0x401": ("Glen", 2, 3), "0x402": ("Heath", 3, 3),
    "0x403": ("Iris", 4, 3), "0x404": ("Juniper", 5, 3), "0x405": ("Kale", 6, 3),
    "0x406": ("Lily", 7, 3), "0x407": ("Maple", 8, 3), "0x408": ("Nettle", 9, 3),
    "0x410": ("Oak", 5, 4), "0x411": ("Pine", 5, 5), "0x412": ("Quince", 4, 6),
    "0x413": ("Rowan", 6, 6),
    "0x501": ("Sage", 1, 2), "0x502": ("Thyme", 1, 4),
    "0x600": ("Quay", 0, 7), "0x601": ("Ring", 1, 7), "0x602": ("Spur", 0, 8),
    "0x603": (None, 1, 8),
    "0x700": ("Vale", 5, 8), "0x701": ("Wren", 6, 8), "0x710": ("Tern", 2, 8),
    "0x711": ("Umber", 3, 8),
}
JUNCTIONS = {"0x303": (5, 0), "0x500": (1, 3)}

LINES = {
    # Meets B at Exchange, which is its last station.
    "A": [("0x101", "0x102"), ("0x102", "0x100")],
    # Mill leaves at the junction 0x303, which has two stations on one side
    # of it along the spine and three on the other: the three decide.
    "B": [("0x100", "0x301"), ("0x301", "0x302"), ("0x302", "0x303"), ("0x303", "0x304"),
          ("0x304", "0x305"), ("0x303", "0x306")],
    # A branch from Juniper that forks again at Pine; C crosses D at the
    # junction 0x500, where neither has a station.
    "C": [("0x400", "0x500"), ("0x500", "0x401"), ("0x401", "0x402"), ("0x402", "0x403"),
          ("0x403", "0x404"), ("0x404", "0x405"), ("0x405", "0x406"), ("0x406", "0x407"),
          ("0x407", "0x408"), ("0x404", "0x410"), ("0x410", "0x411"), ("0x411", "0x412"),
          ("0x411", "0x413")],
    "D": [("0x501", "0x500"), ("0x500", "0x502")],
    # A loop of four stations, one of them unnamed, which S calls at too.
    "E": [("0x600", "0x602"), ("0x602", "0x603"), ("0x603", "0x601"), ("0x601", "0x600")],
    # Two pieces. The unnamed station has the smallest id, so its piece
    # holds the spine and the other is a branch from nowhere.
    "S": [("0x711", "0x710"), ("0x710", "0x603"), ("0x700", "0x701")],
}


def _graph(stations: dict, junctions: dict, lines: dict) -> LineGraph:
    """A line graph with coordinates near Los Angeles, so it can be drawn."""
    def at(x: float, y: float) -> tuple[float, float]:
        return (-118.3 + x * 0.01, 34.0 + y * 0.01)

    nodes = {nid: Node(id=nid, coord=at(x, y), station_id=f"S{nid}", station_label=name)
             for nid, (name, x, y) in stations.items()}
    nodes.update({nid: Node(id=nid, coord=at(x, y)) for nid, (x, y) in junctions.items()})
    edges = [Edge(src=a, dst=b, geometry=[nodes[a].coord, nodes[b].coord],
                  lines=[Line(id=label, label=label, color="#336699")])
             for label, pairs in lines.items() for a, b in pairs]
    return LineGraph(nodes=nodes, edges=edges)


def fixture() -> LineGraph:
    return _graph(STATIONS, JUNCTIONS, LINES)


# Worked by hand from the rules. A spine runs from the node farthest from
# the line's smallest id to the node farthest from that one (linear.spine).
EXPECTED = {"extent": None, "lines": [
    {"label": "A", "termini": ["Alder", "Exchange"], "stations": ["Alder", "Ash", "Exchange"],
     "meets": [{"station": "Exchange", "lines": ["B"]}], "branches": [], "trip": None},
    {"label": "B", "termini": ["Elm", "Exchange"],
     "stations": ["Elm", "Dune", "Cove", "Bay", "Exchange"],
     "meets": [{"station": "Exchange", "lines": ["A"]}],
     "branches": [{"at": "Cove", "stations": ["Mill"]}], "trip": None},
    {"label": "C", "termini": ["Nettle", "Fern"],
     "stations": ["Nettle", "Maple", "Lily", "Kale", "Juniper", "Iris", "Heath", "Glen", "Fern"],
     "meets": [],
     "branches": [{"at": "Juniper", "stations": ["Oak", "Pine"]},
                  {"at": "Pine", "stations": ["Quince"]},
                  {"at": "Pine", "stations": ["Rowan"]}], "trip": None},
    {"label": "D", "termini": ["Thyme", "Sage"], "stations": ["Thyme", "Sage"],
     "meets": [], "branches": [], "trip": None},
    {"label": "E", "termini": ["Quay"], "stations": ["Quay", "Ring", "", "Spur"],
     "meets": [{"station": "", "lines": ["S"]}], "branches": [], "trip": None},
    {"label": "S", "termini": ["Umber", ""], "stations": ["Umber", "Tern", ""],
     "meets": [{"station": "", "lines": ["E"]}],
     "branches": [{"at": None, "stations": ["Wren", "Vale"]}], "trip": None},
]}


def each_station_once(graph: LineGraph, description: dict) -> None:
    """Rule 6: a line's stations and its branches' are its station nodes,
    each once, compared as names counted from the nodes."""
    for line in description["lines"]:
        _adj, nodes = linear.adjacency_for(graph, line["label"])
        want = Counter(station_name(graph.nodes[n]) for n in nodes if graph.nodes[n].is_station)
        got = Counter(line["stations"] + [s for b in line["branches"] for s in b["stations"]])
        assert got == want, line["label"]


# ------------------------------------------------------------ the fixture

def test_the_fixture_is_described_as_worked_by_hand():
    assert describe(fixture()) == EXPECTED


def test_every_station_of_every_line_is_listed_once():
    each_station_once(fixture(), describe(fixture()))


def test_what_the_fixture_does_not_reach():
    """A fork at a junction that ends a branch is left at the nearest station
    back along the branch, or at that branch's own station when it passed
    none, and a branch that passes no station is left out while those
    beyond it are kept; a junction with as many stations on each side is
    left toward the spine's start; a line can have one station."""
    stations = {f"0x8{i:02d}": (f"P{i}", i, 0) for i in range(9)}
    stations.update({"0x810": ("Q1", 4, 1), "0x812": ("R1", 3, 3), "0x813": ("R2", 5, 3),
                     "0x821": ("R3", 6, 2), "0x822": ("R4", 7, 2),
                     "0x900": ("G0", 0, 5), "0x901": ("G1", 1, 5), "0x903": ("G2", 3, 5),
                     "0x904": ("G3", 4, 5), "0x905": ("G4", 2, 6), "0xa00": ("H0", 0, 8)})
    junctions = {"0x811": (4, 2), "0x820": (5, 1), "0x902": (2, 5), "0xa01": (1, 8)}
    spine = [(f"0x8{i:02d}", f"0x8{i + 1:02d}") for i in range(8)]
    lines = {"F": spine + [("0x804", "0x810"), ("0x810", "0x811"), ("0x811", "0x812"),
                           ("0x811", "0x813"), ("0x804", "0x820"), ("0x820", "0x821"),
                           ("0x820", "0x822")],
             "G": [("0x900", "0x901"), ("0x901", "0x902"), ("0x902", "0x903"),
                   ("0x903", "0x904"), ("0x902", "0x905")],
             "H": [("0xa00", "0xa01")]}
    graph = _graph(stations, junctions, lines)
    found = {line["label"]: line for line in describe(graph)["lines"]}
    assert found["F"]["termini"] == ["P8", "P0"]
    assert found["F"]["branches"] == [{"at": "P4", "stations": ["Q1"]},
                                      {"at": "Q1", "stations": ["R1"]},
                                      {"at": "Q1", "stations": ["R2"]},
                                      {"at": "P4", "stations": ["R3"]},
                                      {"at": "P4", "stations": ["R4"]}]
    assert found["G"]["stations"] == ["G3", "G2", "G1", "G0"]
    assert found["G"]["branches"] == [{"at": "G2", "stations": ["G4"]}]
    assert (found["H"]["termini"], found["H"]["stations"]) == (["H0"], ["H0"])
    each_station_once(graph, describe(graph))


def test_the_minutes_names_follow_the_stage_and_the_extent_is_the_longest():
    """A trip's two names are put in the order the line lists them, a name
    the stage does not list keeps the trip's own order, and the extent is
    the longest trip, the first in label order on a tie."""
    minutes = {"A": {"minutes": 7, "from": "Exchange", "to": "Alder"},
               "B": {"minutes": 7, "from": "Nowhere", "to": "Elm"},
               "C": {"minutes": 3, "from": "Rowan", "to": "Nettle"},
               "E": {"minutes": 5, "from": "Quay", "to": "Quay"}}
    found = describe(fixture(), minutes)
    trips = {line["label"]: line["trip"] for line in found["lines"]}
    assert trips == {"A": {"minutes": 7, "from": "Alder", "to": "Exchange"},
                     "B": {"minutes": 7, "from": "Nowhere", "to": "Elm"},
                     "C": {"minutes": 3, "from": "Nettle", "to": "Rowan"},
                     "D": None, "E": {"minutes": 5, "from": "Quay", "to": "Quay"}, "S": None}
    assert found["extent"] == {"minutes": 7, "line": "A", "from": "Alder", "to": "Exchange"}
    # The cache's own values are read, never changed.
    assert minutes["A"] == {"minutes": 7, "from": "Exchange", "to": "Alder"}


# --------------------------------------------------------- schedule.line_runs

def _trip(label: str, *calls: tuple[str | None, int]) -> schedule.Trip:
    """A trip calling at (node id, or None for a stop that matched none,
    seconds after midnight), arriving and leaving at once."""
    return schedule.Trip(trip_id=f"t{id(calls)}", route_label=label, headsign="",
                         calls=[schedule.Call(stop_id=f"s{i}", node_id=node, arrival=t,
                                              departure=t)
                                for i, (node, t) in enumerate(calls)])


NAMES = {"n1": "Alpha", "n2": "Beta", "n3": "Gamma"}


def test_a_line_is_timed_by_the_median_of_its_commonest_trip():
    """Three trips of 30, 30 and 31 minutes and one of 90: the median of the
    four is 30.5 minutes, which is 31 rounded half up; the 90 does not move it."""
    trips = [_trip("A", ("n1", 0), ("n2", m * 60)) for m in (30, 30, 31, 90)]
    assert schedule.line_runs(trips, ["A"], NAMES) == {
        "A": {"minutes": 31, "from": "Alpha", "to": "Beta"}}


def test_a_tie_between_two_groups_goes_to_the_shorter_median_then_the_names():
    """BART's Blue in small: as many trips each way, 64 minutes one way and
    65 the other, is 64. Equal in count and in median, the names decide."""
    trips = [_trip("A", ("n1", 0), ("n2", 64 * 60)), _trip("A", ("n1", 0), ("n2", 64 * 60)),
             _trip("A", ("n2", 0), ("n1", 65 * 60)), _trip("A", ("n2", 0), ("n1", 65 * 60)),
             _trip("B", ("n3", 0), ("n1", 600)), _trip("B", ("n1", 0), ("n3", 600))]
    assert schedule.line_runs(trips, ["A", "B"], NAMES) == {
        "A": {"minutes": 64, "from": "Alpha", "to": "Beta"},
        "B": {"minutes": 10, "from": "Alpha", "to": "Gamma"}}


def test_half_a_minute_rounds_up():
    trips = [_trip("A", ("n1", 0), ("n2", 64 * 60)), _trip("A", ("n1", 0), ("n2", 65 * 60))]
    assert schedule.line_runs(trips, ["A"], NAMES)["A"]["minutes"] == 65


def test_a_trip_runs_between_its_mapped_calls():
    """A first call that matched no node is not where the trip starts; a trip
    that ends where it began has one name twice; a line with no trip that
    day, or none that reaches two mapped stops, has none."""
    trips = [_trip("A", (None, 0), ("n1", 120), ("n2", 1920)),
             _trip("L", ("n1", 0), ("n2", 600), ("n1", 1500)),
             _trip("Z", ("n1", 0), (None, 600))]
    assert schedule.line_runs(trips, ["A", "L", "N", "Z"], NAMES) == {
        "A": {"minutes": 30, "from": "Alpha", "to": "Beta"},
        "L": {"minutes": 25, "from": "Alpha", "to": "Alpha"},
        "N": None, "Z": None}


# --------------------------------------------- through render.stage and a home

@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch home holding one layout of the fixture, the same graph at
    every stage, and an empty cache of minutes. The feed's tables refuse to
    be read: a description that reached them would download the feed."""
    monkeypatch.setenv(config.ENV, str(tmp_path))
    folder = tmp_path / "data" / "graphs" / KEY / LAYOUT
    folder.mkdir(parents=True)
    text = json.dumps(fixture().to_geojson())
    for name in pipeline.STAGE_FILES.values():
        (folder / name).write_text(text, encoding="utf-8")
    (folder / pipeline.META_FILE).write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pipeline, "_MINUTES", {})

    def refuse(*_args, **_kwargs):
        raise AssertionError("the feed's timetable was read")

    monkeypatch.setattr(feeds, "tables", refuse)
    return tmp_path


TRIPS = [_trip("A", ("0x100", 0), ("0x101", 600)), _trip("A", ("0x100", 0), ("0x101", 660)),
         _trip("C", ("0x408", 0), ("0x411", 1800)),
         _trip("E", ("0x600", 0), ("0x603", 600), ("0x600", 1200))]
TIMED = {"A": {"minutes": 11, "from": "Alder", "to": "Exchange"}, "B": None,
         "C": {"minutes": 30, "from": "Nettle", "to": "Pine"}, "D": None,
         "E": {"minutes": 20, "from": "Quay", "to": "Quay"}, "S": None}


@pytest.fixture
def day(monkeypatch):
    """The day's reading stood in for with ``TRIPS``; each read is counted."""
    reads: list[tuple[str, dt.date]] = []

    def schedule_for(found, date, **_kwargs):
        reads.append((found.id, date))
        return pipeline.Day(date=date, graph=LineGraph.from_geojson(found.paths["octi"]),
                            match=schedule.StopMatch(stop_to_node={}, unmatched=[]),
                            trips=TRIPS)

    monkeypatch.setattr(pipeline, "schedule_for", schedule_for)
    return reads


def test_without_a_date_the_minutes_are_null_and_no_timetable_is_read(home):
    for stage in pipeline.STAGE_FILES:
        _svg, _counts, description = render.stage(KEY, stage, layout=LAYOUT)
        assert description == EXPECTED


def test_with_a_date_the_minutes_are_read_once_per_layout_and_day(home, day):
    _svg, _counts, first = render.stage(KEY, "octi", layout=LAYOUT, date=DAY)
    assert {line["label"]: line["trip"] for line in first["lines"]} == TIMED
    assert first["extent"] == {"minutes": 30, "line": "C", "from": "Nettle", "to": "Pine"}
    assert day == [(LAYOUT, DAY)]
    for stage in ("gtfs2graph", "topo", "loom", "octi"):
        _svg, _counts, again = render.stage(KEY, stage, layout=LAYOUT, date=DAY)
        assert again == first
    assert day == [(LAYOUT, DAY)]
    render.stage(KEY, "octi", layout=LAYOUT, date=DAY + dt.timedelta(days=1))
    assert day == [(LAYOUT, DAY), (LAYOUT, DAY + dt.timedelta(days=1))]
    # The minutes are kept, and nothing else: never the trips, never the tables.
    assert set(pipeline._MINUTES) == {(LAYOUT, DAY), (LAYOUT, DAY + dt.timedelta(days=1))}
    for per_line in pipeline._MINUTES.values():
        assert set(per_line) == set(LINES)
        for value in per_line.values():
            assert value is None or (
                set(value) == {"minutes", "from", "to"} and type(value["minutes"]) is int
                and type(value["from"]) is str and type(value["to"]) is str), value


@pytest.fixture
def client():
    c = Client()
    yield c
    c.endpoint.close()


def test_over_the_protocol_the_description_is_answered_with_and_without_a_date(home, day,
                                                                                client):
    for params, date in (({}, None), ({"date": DAY.isoformat()}, DAY)):
        response = client.call("render.stage", {"key": KEY, "layout": LAYOUT, "stage": "octi",
                                                "width": 600, **params})
        assert "result" in response, response
        result = response["result"]
        check(result, "RenderStageResult")
        svg, counts, description = render.stage(KEY, "octi", layout=LAYOUT, width=600,
                                                date=date)
        assert result["svg"] == svg
        assert {**result["counts"], "width": result["width"],
                "height": result["height"]} == counts
        assert result["description"] == description
    assert result["description"]["extent"]["line"] == "C"
    error = client.call("render.stage", {"key": KEY, "layout": LAYOUT, "stage": "octi",
                                         "date": None})["error"]
    assert error["code"] == -32602 and error["data"]["kind"] == "params"
    assert "or left out" in error["data"]["hint"]


# ------------------------------------------------------ over stored layouts

def _newest(key: str) -> pipeline.Layout:
    """The newest stored layout of a feed, never through ``pipeline.stored``,
    which may migrate a flat set it finds."""
    found = pipeline.stored_layouts(key)
    if not found:
        pytest.skip(f"needs a stored {key} layout")
    return found[0]


def test_map_build_fills_the_cache(tmp_path, monkeypatch):
    """After a draw of a layout and day, a description of the same reads no
    timetable: the draw already held the day's trips."""
    stored = _newest("pittsburgh-t")
    monkeypatch.setattr(pipeline, "_MINUTES", {})
    when = dt.date(2026, 9, 10)
    pipeline.run("pittsburgh-t", layout=stored.id, date=when, out_dir=tmp_path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the feed's timetable was read again")

    monkeypatch.setattr(feeds, "tables", refuse)
    _svg, _counts, description = render.stage("pittsburgh-t", "octi", layout=stored.id,
                                              date=when)
    assert description["extent"] is not None
    assert description["extent"]["minutes"] > 0


def test_bart_on_a_friday(monkeypatch):
    """The criterion of engine issue 54, on the newest stored BART layout's
    octi stage and the day the busiest weekday rule picks: Blue runs 58 trips
    each way, 64 minutes from Daly City and 65 back, and the shorter wins."""
    stored = _newest("bart")
    monkeypatch.setattr(pipeline, "_MINUTES", {})
    _svg, _counts, description = render.stage("bart", "octi", layout=stored.id, date=DAY)
    found = {line["label"]: line for line in description["lines"]}
    assert found["Blue"]["termini"] == ["Daly City", "Dublin / Pleasanton"]
    assert found["Blue"]["trip"] == {"minutes": 64, "from": "Daly City",
                                     "to": "Dublin / Pleasanton"}
    assert description["extent"]["minutes"] == 104
    assert description["extent"]["line"] == "Yellow"
    for label in ("Red", "Yellow"):
        assert found[label]["branches"] == [
            {"at": "San Bruno", "stations": ["Millbrae (Caltrain Transfer Platform)"]}]
    for label in ("BridgeA", "BridgeB"):
        assert len(found[label]["termini"]) == 1
        assert found[label]["branches"] == []
    each_station_once(LineGraph.from_geojson(stored.paths["octi"]), description)
