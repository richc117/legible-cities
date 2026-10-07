"""``map.build``'s ``style``: the numbers and colours a client may draw the map with.

``render.STYLE_RANGES`` is the one table; the server's sentences
(``serve._style``) and the protocol schema's ``MapStyle`` agree with it, and
the first test holds the three together. Omitting ``style`` -- or sending
``{}`` -- draws exactly what is drawn without the parameter, which is
``Style(themed=True)``: ``Style(**fields)`` alone is not that, because
``themed`` defaults to false and drops every ``var(--map-*)`` from the page.
The drawing tests use the invented twelve-station network the theme pictures
are made from, built in code, so they run without a feed, LOOM or a stored
layout; the last test is the one that needs a stored layout.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
from types import SimpleNamespace

import pytest
from pylsp_jsonrpc.exceptions import JsonRpcInvalidParams
from test_colors import _graph, _stored
from test_serve import DATE, KEY, NO_LAYOUT, SCHEMA, Client, check, invalid

from schematic import animate, config, diagnostics, pipeline, serve
from schematic.render import STYLE_RANGES, Style, render
from schematic.theme_thumbnails import fixture

# The ranges the issue fixes, written out here and not read from the table, so
# a bound loosened in ``STYLE_RANGES`` alone is a failure of the tests and not
# a new truth for them.
RANGES = {
    "line_width": (1, 24),
    "line_gap": (1, 3),
    "station_radius": (1, 20),
    "interchange_radius": (1, 30),
    "station_stroke": (0, 8),
    "label_size": (6, 32),
    "label_offset": (0, 40),
    "padding": (0, 200),
}
COLORS = ("background", "station_fill", "station_stroke_color", "label_color")
UNIT = "SVG user units at the map's width"
# ``interchange_radius`` may not be below ``station_radius``, so an end of one
# of the two is sent with the partner that keeps the pair valid.
PARTNER = {("station_radius", 20): {"interchange_radius": 30},
           ("interchange_radius", 1): {"station_radius": 1}}

ENDS = [(name, end) for name, bounds in RANGES.items() for end in bounds]


@pytest.fixture
def client():
    c = Client()
    yield c
    c.endpoint.close()


def refusal(style) -> dict:
    """The error data ``serve._style`` refuses ``style`` with."""
    with pytest.raises(JsonRpcInvalidParams) as refused:
        serve._style(style)
    data = refused.value.data
    assert data["kind"] == "params"
    return data


def draw(style: Style | None = None, labels: bool = True) -> str:
    """The invented network at the width its own pictures use."""
    return render(fixture(), width=448, style=style or Style(themed=True), labels=labels).svg


def box(svg: str, attribute: str = "viewBox") -> tuple[float, float, float, float]:
    found = re.search(rf'{attribute}="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"', svg)
    assert found, f"no {attribute} in the drawing"
    return tuple(float(v) for v in found.groups())


def nolabels(svg: str) -> tuple[float, float, float, float]:
    return box(svg, "data-viewbox-nolabels")


def line_widths(svg: str) -> list[str]:
    return re.findall(r'<g class="line"[^>]*stroke-width="([^"]+)"', svg)


def circles(svg: str) -> set[tuple[str, str]]:
    return set(re.findall(r'<circle cx="([^"]+)" cy="([^"]+)"', svg))


# ------------------------------------------------- the table and the schema

def test_the_table_and_the_schema_agree():
    defs = SCHEMA["$defs"]
    style = defs["MapStyle"]
    props = style["properties"]
    assert defs["MapBuildParams"]["properties"]["style"] == {"$ref": "#/$defs/MapStyle"}
    assert style["type"] == "object" and style["additionalProperties"] is False
    assert "required" not in style
    assert set(props) == set(RANGES) | set(COLORS)
    assert {name: bounds[:2] for name, bounds in STYLE_RANGES.items()} == RANGES

    defaults = Style()
    for name, (low, high, unit) in STYLE_RANGES.items():
        prop = props[name]
        assert prop["type"] == "number"
        assert (prop["minimum"], prop["maximum"]) == (low, high)
        # Every default lies inside its range, and the description says what it is.
        assert low <= getattr(defaults, name) <= high
        assert f"{getattr(defaults, name):g} when omitted" in prop["description"]
        # One unit for all but ``line_gap``, a multiple of the line width.
        assert unit == (None if name == "line_gap" else UNIT)
        assert (unit or "multiple of line_width") in prop["description"]
    for name in COLORS:
        prop = props[name]
        assert prop["$ref"] == "#/$defs/HexColor"
        assert "falls back to" in prop["description"]
        assert "theme overrides it" in prop["description"]
        assert f"{getattr(defaults, name)} when omitted" in prop["description"]
    # What the schema cannot hold is said where a client reads.
    assert "interchange_radius may not be below station_radius" in style["description"]


# ------------------------------------------------------- both ends of a field

@pytest.mark.parametrize("name,end", ENDS)
def test_each_end_of_every_field_is_accepted(name, end):
    style = serve._style({name: end, **PARTNER.get((name, end), {})})
    assert getattr(style, name) == end
    assert style.themed
    check({"key": KEY, "layout": NO_LAYOUT, "date": DATE,
           "style": {name: end, **PARTNER.get((name, end), {})}}, "MapBuildParams")


@pytest.mark.parametrize("name,end", ENDS)
def test_a_tenth_past_each_end_is_refused_naming_the_field_and_its_range(name, end):
    low, high = RANGES[name]
    past = round(end - 0.1 if end == low else end + 0.1, 1)
    hint = refusal({name: past})["hint"]
    where = "as a multiple of line_width" if name == "line_gap" else f"in {UNIT}"
    assert hint == f"style.{name} must be from {low} to {high}, {where}"
    assert invalid({"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": {name: past}},
                   "MapBuildParams")


@pytest.mark.parametrize("wrong", [
    {"line_width": True}, {"line_width": "7"}, {"line_width": None}, {"line_width": [7]},
    {"padding": float("nan")}, {"padding": float("inf")},
    {"background": None}, {"background": 0}, {"background": "black"}, {"label_color": "#12345"},
    {"themed": True}, {"label_char_width": 0.5}, {"default_line_color": "#888888"},
    {"default_color": "#888888"}, {"width": 100},
])
def test_what_is_not_a_number_a_colour_or_a_field_is_refused(wrong):
    refusal(wrong)


@pytest.mark.parametrize("wrong", ["thin", ["line_width"], 7, True])
def test_a_style_that_is_not_an_object_is_refused(wrong):
    assert refusal(wrong)["hint"] == "style must be an object"


def test_an_unknown_field_is_named():
    assert refusal({"themed": True, "label_char_width": 0.5})["hint"] == \
        "style does not take label_char_width, themed"


# ----------------------------------------------------------------- the cross-check

def test_interchange_radius_below_station_radius_is_refused_by_hand(client):
    """The schema cannot hold it, so only the server refuses; judged on the
    values the map would be drawn with, a field left out being its default."""
    for style, shown in (({"station_radius": 8, "interchange_radius": 5}, ("(5)", "(8)")),
                         ({"station_radius": 8}, ("(6)", "(8)")),
                         ({"interchange_radius": 3}, ("(3)", "(4.2)"))):
        hint = refusal(style)["hint"]
        assert "style.station_radius" in hint and "style.interchange_radius" in hint
        assert all(value in hint for value in shown), hint
        params = {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": style}
        check(params, "MapBuildParams")
        error = client.call("map.build", params)["error"]
        assert error["code"] == -32602 and error["data"]["kind"] == "params"
        assert error["data"]["hint"] == hint
    assert serve._style({"station_radius": 6, "interchange_radius": 6}).interchange_radius == 6
    assert serve._style({"station_radius": 6}).station_radius == 6


# ---------------------------------------------------------- what is drawn today

def test_omitted_and_empty_draw_today():
    assert serve._style(None) is None
    assert serve._style({}) == Style(themed=True)
    assert draw(serve._style({})) == draw(Style(themed=True))
    # Themed whatever is asked: the page's variables are in the furniture.
    assert "var(--map-bg, #ffffff)" in draw(serve._style({"line_width": 12}))


# ---------------------------------------------------- the request reaches the pipeline

def test_the_style_reaches_the_pipeline_and_its_absence_does_too(client, tmp_path, monkeypatch):
    """With ``pipeline.run`` stood in for by a hand-made graph, so it runs
    without a feed or LOOM: what ``map.build`` hands it is the style asked
    for, built themed, and nothing when none was asked for."""
    monkeypatch.setenv(config.ENV, str(tmp_path))
    graph = _graph([[("A", "0072bc"), ("B", None)], [("B", None), ("C", "ff0000")]])
    diag = diagnostics.Diagnostics(
        key="p9", name="Nine", date=dt.date.fromisoformat(DATE), stations=3, junctions=0,
        edges=2, lines=("A", "B", "C"), octilinear=1.0,
        stops=diagnostics.StopMatching(3, 3, 3, 0, 0, ()), trips_total=0, paths=0, unrouted=0,
        skipped_calls=0, borrowed_track=0, labels_dropped=0, peak_concurrent=0)
    asked: list[dict] = []

    def stood_in(key, **kwargs):
        asked.append(kwargs)
        kwargs["out_dir"].mkdir(parents=True)
        return SimpleNamespace(layout=NO_LAYOUT, date=dt.date.fromisoformat(DATE), graph=graph,
                               diagnostics=lambda: diag)

    monkeypatch.setattr(pipeline, "run", stood_in)
    styled = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "out": "s1",
                                       "style": {"line_width": 12, "background": "#000000"}})
    plain = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                                      "out": "s2"})
    for response in (styled, plain):
        assert "result" in response, response
        check(response["result"], "MapBuildResult")
    assert asked[0]["style"] == Style(themed=True, line_width=12, background="#000000")
    assert asked[0]["style"] != Style(line_width=12, background="#000000")
    assert asked[1]["style"] is None


# ------------------------------------------------------------ what a style draws

def test_a_wider_line_moves_the_strokes_and_no_station():
    plain, wide = draw(), draw(serve._style({"line_width": 12}))
    assert line_widths(plain) == ["7.00"] * 4
    assert line_widths(wide) == ["12.00"] * 4
    assert len(circles(plain)) == 12
    assert circles(wide) == circles(plain)


def test_label_size_and_offset_re_place_the_labels_and_move_the_viewbox_not_the_networks():
    """The network's own box is left out of the labels on purpose: the page's
    label toggle swaps one box for the other. Measured at width 448:
    ``viewBox`` -31.00 -32.58 552.75 319.58 becomes -31.00 -56.56 586.23
    343.56 for a label size of 20, and the network's box stays put."""
    plain, big = draw(), draw(serve._style({"label_size": 20}))
    assert re.search(r'<g id="labels" font-size="11\.0"', plain)
    assert re.search(r'<g id="labels" font-size="20\.0"', big)
    assert box(big) != box(plain)
    assert box(big)[2] > box(plain)[2] and box(big)[3] > box(plain)[3]
    assert nolabels(big) == nolabels(plain) == (-31.0, -31.0, 510.0, 318.0)

    far = draw(serve._style({"label_offset": 30}))
    assert box(far) != box(plain)
    assert nolabels(far) == nolabels(plain)


def test_padding_grows_both_boxes_on_every_side():
    """Forty against the default twenty-four: each box starts 16 earlier and
    is 32 larger on both axes."""
    plain, padded = draw(), draw(serve._style({"padding": 40}))
    for read in (box, nolabels):
        (x, y, w, h), (px, py, pw, ph) = read(plain), read(padded)
        assert (px, py) == pytest.approx((x - 16, y - 16), abs=0.011)
        assert (pw, ph) == pytest.approx((w + 32, h + 32), abs=0.011)


# ------------------------------------------------------ over a stored layout

def test_a_wider_line_reaches_the_map_the_page_and_the_geographic_layer(tmp_path, monkeypatch):
    """Pittsburgh from its stored layout, drawn as it always was and with a
    line width of 12: the four stage files are not touched by either run, the
    strokes (in the SVG and in the page that embeds it) carry the width, and
    the geographic layer is spaced by the style the map is drawn with -- the
    morph squeezes every bundle otherwise."""
    stored = _stored("pittsburgh-t")
    day = dt.date(2026, 9, 10)

    def stages() -> dict[str, str]:
        return {stage: hashlib.sha256(path.read_bytes()).hexdigest()
                for stage, path in stored.paths.items()}

    before = stages()
    handed: list[Style | None] = []
    real = animate.geographic_tracks

    def spy(geo_graph, ref_graph, ref, style=None):
        handed.append(style)
        return real(geo_graph, ref_graph, ref, style)

    monkeypatch.setattr(animate, "geographic_tracks", spy)
    plain = pipeline.run("pittsburgh-t", layout=stored.id, date=day, out_dir=tmp_path / "plain")
    wide = pipeline.run("pittsburgh-t", layout=stored.id, date=day, out_dir=tmp_path / "wide",
                        style=Style(themed=True, line_width=12))

    assert stages() == before
    assert set(line_widths(plain.render.svg)) == {"7.00"}
    assert set(line_widths(wide.render.svg)) == {"12.00"}
    assert line_widths((tmp_path / "wide" / "pittsburgh-t.svg").read_text()) == \
        line_widths(wide.render.svg)
    assert 'stroke-width="12.00"' in (tmp_path / "wide" / "pittsburgh-t.html").read_text()
    # The tracks are the same stored geometry, apart from where a bundle's
    # lines sit beside each other.
    assert circles(wide.render.svg) == circles(plain.render.svg)
    assert len(handed) == 2 and None not in handed
    assert [style.spacing for style in handed] == [7.0 * 1.6, 12 * 1.6]
