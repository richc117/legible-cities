"""``map.build``'s ``style``: the numbers and colours a client may draw the map with.

``render.STYLE_RANGES`` is the one table; the server's sentences
(``serve._style``) and the protocol schema's ``MapStyle`` agree with it, and
the first test holds the three together. Omitting ``style`` -- or sending
``{}`` -- draws exactly what is drawn without the parameter, which is
``Style(themed=True)``: ``Style(**fields)`` alone is not that, because
``themed`` defaults to false and drops every ``var(--map-*)`` from the page.
The drawing tests use the invented twelve-station network the theme pictures
are made from, built in code, so they run without a feed, LOOM or a stored
layout; the two tests over Pittsburgh's stored layout skip without it.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import math
import re
from types import SimpleNamespace

import pytest
from pylsp_jsonrpc.exceptions import JsonRpcInvalidParams
from test_colors import _graph, _stored
from test_serve import DATE, KEY, NO_LAYOUT, SCHEMA, Client, check, invalid

from schematic import animate, config, diagnostics, pipeline, serve
from schematic import render as render_module
from schematic.labels import Quad, collide
from schematic.linegraph import Edge, Line, LineGraph, Node
from schematic.render import PRESETS, STYLE_RANGES, STYLE_SHAPES, Style, preset_style, render
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
# The three presets as issue 73 decided them, in ``STYLE_RANGES``' order and
# written out here, not read from ``render.PRESETS``, so a number changed in the
# table alone is a failure of the tests and not a new truth for them.
PRESET_NUMBERS = {
    "beck": (6, 1.33, 3.6, 7.5, 3, 11, 10, 24),
    "blueprint": (4, 2, 3, 4.5, 1.5, 10, 8, 32),
    "paper": (6, 1.6, 3.6, 5.5, 1.8, 12, 10, 28),
}
# The marker shapes issue 74 settled, each field's names in order, and the one
# preset that names a shape: beck, TfL's tick. Written out, as the numbers are.
SHAPES = {"station_shape": ("circle", "tick", "square"),
          "interchange_shape": ("circle", "square")}
PRESET_SHAPES = {"beck": {"station_shape": "tick"}}
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
    assert set(props) == set(RANGES) | set(COLORS) | set(SHAPES) | {"preset", "label_font"}
    # The server's own list of colour fields is held to the schema too, so a
    # fifth colour added to the server alone cannot be accepted where the
    # schema refuses it, the one direction the hand-validation test cannot see.
    assert set(serve.STYLE_COLORS) == set(COLORS)
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
    for name, names in SHAPES.items():
        prop = props[name]
        assert prop["enum"] == list(names) == list(STYLE_SHAPES[name])
        assert getattr(defaults, name) == "circle"
        assert "circle when omitted" in prop["description"]
    assert STYLE_SHAPES == SHAPES
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

def stand_in_pipeline(monkeypatch, tmp_path) -> list[dict]:
    """``pipeline.run`` stood in for by a hand-made graph, so ``map.build``
    runs without a feed or LOOM; the list is what each call was handed."""
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
    return asked


def test_the_style_reaches_the_pipeline_and_its_absence_does_too(client, tmp_path, monkeypatch):
    """With ``pipeline.run`` stood in for by a hand-made graph, so it runs
    without a feed or LOOM: what ``map.build`` hands it is the style asked
    for, built themed, and nothing when none was asked for."""
    asked = stand_in_pipeline(monkeypatch, tmp_path)
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


# ------------------------------------------------------------------ the presets

def test_the_presets_are_the_three_the_issue_decided_in_its_order():
    assert list(PRESETS) == ["beck", "blueprint", "paper"]
    for name, numbers in PRESET_NUMBERS.items():
        shapes = PRESET_SHAPES.get(name, {})
        assert PRESETS[name] == {**dict(zip(STYLE_RANGES, numbers)), **shapes}, name
        # All eight, then a shape where the look has its own, and nothing else:
        # no colour, which the theme owns.
        assert list(PRESETS[name]) == list(STYLE_RANGES) + list(shapes), name


def test_every_preset_value_is_inside_its_range_with_the_interchange_above_the_station():
    for name, numbers in PRESETS.items():
        for field, value in numbers.items():
            if field in SHAPES:
                assert value in SHAPES[field], f"{name}.{field} is {value!r}"
                continue
            low, high = RANGES[field]
            assert low <= value <= high, f"{name}.{field} is {value}, outside {low} to {high}"
        assert numbers["interchange_radius"] > numbers["station_radius"], name


def test_no_preset_equals_the_default_or_another():
    """By the eight numbers alone: beck's tick is a ninth key, and a preset
    whose numbers were the default's would differ from it by that key only."""
    def eight(fields: dict) -> dict:
        return {k: v for k, v in fields.items() if k in STYLE_RANGES}

    default = {field: getattr(Style(), field) for field in STYLE_RANGES}
    for name, numbers in PRESETS.items():
        assert eight(numbers) != default, f"{name} is the default style"
    named = list(PRESETS.items())
    for i, (one, first) in enumerate(named):
        for other, second in named[i + 1:]:
            assert eight(first) != eight(second), f"{one} and {other} are the same style"


def test_preset_style_answers_a_copy_and_refuses_what_is_not_a_name():
    got = preset_style("beck")
    assert got == PRESETS["beck"]
    got["line_width"] = 99
    assert PRESETS["beck"]["line_width"] == 6, "the answer is the table's own dict"
    assert preset_style("beck") == PRESETS["beck"]
    with pytest.raises(KeyError):
        preset_style("night")


# The sentences a client reads, written out here and not built from the table.
BOTH = "style.preset cannot be sent with the fields it resolves to; send one or the other"
UNKNOWN = "style.preset must be beck, blueprint or paper; style.presets describes each"


def numbers(name: str) -> dict:
    """The preset's fields as the issues wrote them, never read from the table:
    its eight numbers, and beck's tick."""
    return {**dict(zip(RANGES, PRESET_NUMBERS[name])), **PRESET_SHAPES.get(name, {})}


def test_the_schema_knows_the_presets_by_name_and_by_their_numbers():
    defs = SCHEMA["$defs"]
    assert defs["MapStyle"]["properties"]["preset"]["enum"] == list(PRESETS)
    assert defs["StylePreset"]["properties"]["name"]["enum"] == list(PRESETS)
    assert SCHEMA["methods"]["style.presets"] == {
        "params": {"$ref": "#/$defs/NoParams"}, "result": {"$ref": "#/$defs/StylePresets"}}
    assert defs["StylePresets"]["properties"]["presets"]["items"] == {"$ref": "#/$defs/StylePreset"}
    # The eight, complete and closed, inside the bounds MapStyle sends them with,
    # and the two shapes, which a preset may name and need not.
    style = defs["StylePreset"]["properties"]["style"]
    assert style["additionalProperties"] is False
    assert list(style["properties"]) == list(RANGES) + list(SHAPES)
    assert style["required"] == list(RANGES)
    for field, names in SHAPES.items():
        assert style["properties"][field] == {"enum": list(names)}
    for field, (low, high) in RANGES.items():
        assert style["properties"][field] == {"type": "number", "minimum": low, "maximum": high}
        mapped = defs["MapStyle"]["properties"][field]
        assert (mapped["minimum"], mapped["maximum"]) == (low, high)
    # A preset says it is sent alone, where a client reads.
    assert "refused" in defs["MapStyle"]["properties"]["preset"]["description"]


def test_a_name_resolves_to_exactly_its_numbers_built_themed():
    for name in PRESET_NUMBERS:
        style = serve._style({"preset": name})
        assert style == Style(themed=True, **numbers(name)), name
        assert style.themed
        # Nothing the preset does not name moves: colours and the engine's own fields.
        assert style.background == Style().background
        assert style.label_char_width == Style().label_char_width
        # The same numbers sent one by one are the same style.
        assert style == serve._style(numbers(name))


def test_a_name_and_its_numbers_draw_the_same_bytes_and_the_three_differ():
    """On the invented network, so it runs anywhere; the stored layout's
    version of this is the last test."""
    by_name = {name: draw(serve._style({"preset": name})) for name in PRESET_NUMBERS}
    by_numbers = {name: draw(serve._style(numbers(name))) for name in PRESET_NUMBERS}
    assert by_name == by_numbers
    assert len({*by_name.values(), draw()}) == 4, "the three differ, and from the default"


@pytest.mark.parametrize("beside", [
    {"line_width": 6}, {"padding": 24, "label_size": 11}, {"background": "#000000"},
    {"themed": True}, {"unheard_of": 1}, {"line_width": 99},
])
def test_a_preset_beside_any_other_field_is_refused(beside):
    """Whatever the other field is, valid or not, known or not: the name is
    sent alone, so which of the two would win is never left to a guess."""
    assert refusal({"preset": "beck", **beside})["hint"] == BOTH


def test_a_preset_beside_a_field_is_refused_before_its_name_is_read():
    assert refusal({"preset": "night", "padding": 24})["hint"] == BOTH


@pytest.mark.parametrize("name", [
    "night", "Beck", "beck ", "", "default", None, 3, True, ["beck"], {"beck": 1},
])
def test_an_unknown_preset_is_refused_naming_the_three(name):
    assert refusal({"preset": name})["hint"] == UNKNOWN


def test_the_schema_refuses_an_unknown_name_and_takes_each_known_one():
    for name in PRESET_NUMBERS:
        check({"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": {"preset": name}},
              "MapBuildParams")
    for name in ("night", "Beck", "", None, 3):
        assert invalid({"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                        "style": {"preset": name}}, "MapBuildParams"), name


def test_map_build_resolves_the_name_before_the_pipeline_and_refuses_both(
        client, tmp_path, monkeypatch):
    """A name reaches ``pipeline.run`` as the style its numbers make, the
    same as the numbers sent one by one; the refusals are the wire's own."""
    asked = stand_in_pipeline(monkeypatch, tmp_path)
    for i, name in enumerate(PRESET_NUMBERS):
        for style in ({"preset": name}, numbers(name)):
            sent = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                                             "out": f"p{i}-{len(asked)}", "style": style})
            assert "result" in sent, sent
            check(sent["result"], "MapBuildResult")
            assert asked[-1]["style"] == Style(themed=True, **numbers(name))
    assert len(asked) == 6
    for style, hint in (({"preset": "beck", "line_width": 6}, BOTH),
                        ({"preset": "night"}, UNKNOWN)):
        error = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                                          "style": style})["error"]
        assert error["code"] == -32602 and error["data"]["kind"] == "params"
        assert error["data"]["hint"] == hint
    assert len(asked) == 6, "a refused request never reaches the pipeline"


def test_style_presets_answers_each_name_with_numbers_a_client_can_send(client):
    result = client.call("style.presets")["result"]
    check(result, "StylePresets")
    assert [p["name"] for p in result["presets"]] == ["beck", "blueprint", "paper"]
    for answered in result["presets"]:
        name, style = answered["name"], answered["style"]
        assert style == numbers(name), name
        # Sent field by field, it is accepted and is the style the name makes.
        check({"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": style}, "MapBuildParams")
        assert serve._style(style) == serve._style({"preset": name})
    # No params, as the schema says: nothing, or an empty object.
    assert client.call("style.presets", {})["result"] == result
    assert client.call("style.presets", {"preset": "beck"})["error"]["code"] == -32602
    assert client.call("style.presets", ["beck"])["error"]["code"] == -32602


def test_over_a_stored_layout_a_name_and_its_numbers_write_byte_identical_maps(tmp_path):
    """Pittsburgh from its stored layout, once per preset by name and once by
    the numbers ``style.presets`` answered: the SVG on the wire's side and the
    one written to disk are the same bytes, and the three presets and the
    default are four different maps. Skips where the layout is not stored."""
    stored = _stored("pittsburgh-t")
    day = dt.date(2026, 9, 10)

    def drawn(label: str, style: dict | None) -> str:
        folder = tmp_path / label
        result = pipeline.run("pittsburgh-t", layout=stored.id, date=day, out_dir=folder,
                              style=serve._style(style))
        written = (folder / "pittsburgh-t.svg").read_text()
        assert written == result.render.svg, label
        return written

    client = Client()
    try:
        answered = {p["name"]: p["style"]
                    for p in client.call("style.presets")["result"]["presets"]}
    finally:
        client.endpoint.close()
    assert list(answered) == ["beck", "blueprint", "paper"]

    maps = {}
    for name, style in answered.items():
        by_name = drawn(f"{name}-by-name", {"preset": name})
        assert by_name == drawn(f"{name}-by-numbers", style), name
        maps[name] = by_name
    maps["default"] = drawn("default", None)
    assert len(set(maps.values())) == 4, "the three differ from each other and from the default"
    # Each preset's own stroke width is what the strokes carry.
    for name in answered:
        assert set(line_widths(maps[name])) == {f"{PRESET_NUMBERS[name][0]:.2f}"}, name


# ------------------------------------------------------ the markers (issue 74)
#
# A station's marker is a circle, TfL's tick or a square turned with its line;
# an interchange's is a circle or a rounded square, never a tick. A circle is
# written as it always was; a tick or a square is a path whose first moveto is
# the station and whose outline is relative to it, and says which it is in
# data-shape. Drawn on the invented network, and on one line that runs east,
# bends to the north-east and bends again to the north, so a tick is seen
# square to a straight line, bisecting a bend, and across a line's end.

# The fixture's drawing in the default style, labels on, as the commit before
# issue 74 wrote it: what a map of circles must still be, byte for byte.
BEFORE_74 = "64fb020f643d2d0cdb887569387fe19b8b20e0acd2ccff2897c0702061ca41dc"
# TfL's tick, written out: 0.66 of the line's width on a side, past its edge.
TICK_OF = 0.66
BENDS = [("k0", 0, 0), ("k1", 3, 0), ("k2", 6, 0), ("k3", 9, 3), ("k4", 12, 6),
         ("k5", 12, 9), ("k6", 12, 12)]


def bends() -> LineGraph:
    """One line, K, through the seven stations of ``BENDS`` in order."""
    nodes = {nid: Node(id=nid, coord=(float(x), float(y)), station_id=nid,
                       station_label=f"Stop {nid[1:]}") for nid, x, y in BENDS}
    edges = [Edge(src=a, dst=b, geometry=[nodes[a].coord, nodes[b].coord],
                  lines=[Line(id="K", label="K", color="#2f6fd6")])
             for (a, *_), (b, *_) in zip(BENDS, BENDS[1:])]
    return LineGraph(nodes=nodes, edges=edges)


def outline(d: str) -> tuple[tuple[float, float], list[tuple[float, float]], list[tuple]]:
    """A marker's path read as the page reads it: the station (its first
    moveto), the points its outline visits in the drawing's coordinates, and
    each arc's radii and flags."""
    words = d.split()
    assert words[0] == "M" and words[3] == "m" and words[-1] == "z", d
    x, y = float(words[1]), float(words[2])
    at, points, arcs, i = (x, y), [], [], 3
    while words[i] != "z":
        if words[i] == "a":
            arcs.append(tuple(float(v) for v in words[i + 1:i + 6]))
            i += 5
        else:
            assert words[i] in ("m", "l"), d
        x, y = x + float(words[i + 1]), y + float(words[i + 2])
        points.append((x, y))
        i += 3
    return at, points, arcs


def markers(svg: str) -> dict[str, dict]:
    """Every station's marker, by node, in the order the map writes them: its
    shape (a marker without data-shape is a circle), where its station is, its
    attributes, and a path's outline."""
    start = svg.index('<g id="stations">')
    found = {}
    for tag, attrs in re.findall(r"<(circle|path) ([^>]*)/>", svg[start:svg.index("</g>", start)]):
        a = dict(re.findall(r'([\w-]+)="([^"]*)"', attrs))
        m = {"tag": tag, "shape": a.get("data-shape", "circle"), "attrs": a}
        if tag == "circle":
            m["at"] = (float(a["cx"]), float(a["cy"]))
        else:
            m["at"], m["points"], m["arcs"] = outline(a["d"])
        found[a["data-node"]] = m
    return found


def tracks(svg: str) -> list[str]:
    return re.findall(r'<path id="[^"]+" data-src="[^"]*" data-dst="[^"]*" d="[^"]+"/>', svg)


def label_at(svg: str, node: str) -> tuple[float, float]:
    found = re.search(rf'<text x="([-\d.]+)" y="([-\d.]+)"[^>]*data-node="{node}"', svg)
    assert found, f"no label for {node}"
    return float(found[1]), float(found[2])


def sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def dot(a, b):
    return a[0] * b[0] + a[1] * b[1]


def unit(v):
    length = math.hypot(*v)
    return (v[0] / length, v[1] / length)


def shaped(**fields) -> Style:
    return Style(themed=True, **fields)


@pytest.mark.parametrize("name,shape", [(n, s) for n, names in SHAPES.items() for s in names])
def test_each_shape_is_taken_by_name(name, shape):
    style = serve._style({name: shape})
    assert getattr(style, name) == shape and style.themed
    check({"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": {name: shape}},
          "MapBuildParams")


@pytest.mark.parametrize("name,wrong", [
    ("station_shape", "diamond"), ("station_shape", "Tick"), ("station_shape", "tick "),
    ("station_shape", ""), ("station_shape", None), ("station_shape", 1),
    ("station_shape", ["tick"]), ("interchange_shape", "tick"), ("interchange_shape", "ring"),
    ("interchange_shape", True),
])
def test_a_shape_not_on_the_list_is_refused_naming_the_list(name, wrong):
    """A tick is not an interchange's shape: it has no side where lines meet."""
    hint = refusal({name: wrong})["hint"]
    assert hint == {"station_shape": "style.station_shape must be circle, tick or square",
                    "interchange_shape": "style.interchange_shape must be circle or square"}[name]
    assert invalid({"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": {name: wrong}},
                   "MapBuildParams")


def test_beck_is_a_tick_with_the_ring_for_an_interchange():
    style = serve._style({"preset": "beck"})
    assert (style.station_shape, style.interchange_shape) == ("tick", "circle")
    drawn = markers(draw(style))
    assert {node: m["shape"] for node, m in drawn.items()} == {
        node: "circle" if node in ("x5y5", "x11y5") else "tick" for node in drawn}
    for name in ("blueprint", "paper"):
        assert {m["shape"] for m in markers(draw(serve._style({"preset": name}))).values()} == \
            {"circle"}, name


def test_circles_are_written_byte_for_byte_as_before():
    plain = draw()
    assert hashlib.sha256(plain.encode()).hexdigest() == BEFORE_74
    assert draw(shaped(station_shape="circle", interchange_shape="circle")) == plain
    assert draw(serve._style({"station_shape": "circle", "interchange_shape": "circle"})) == plain
    assert "data-shape" not in plain


@pytest.mark.parametrize("station", SHAPES["station_shape"])
@pytest.mark.parametrize("interchange", SHAPES["interchange_shape"])
def test_every_shape_keeps_each_station_where_its_circle_was_and_moves_no_track(
        station, interchange):
    """The two interchanges take the interchange's shape and the other ten the
    station's; each marker's station is its circle's centre, and the lines are
    the same bytes."""
    plain = draw()
    drawn = draw(shaped(station_shape=station, interchange_shape=interchange))
    assert tracks(drawn) == tracks(plain) and len(tracks(plain)) == 11
    before, after = markers(plain), markers(drawn)
    assert list(after) == list(before) and len(after) == 12
    for node, marker in after.items():
        assert marker["at"] == before[node]["at"], node
        middle = before[node]["attrs"]["r"] == "6.00"
        assert marker["shape"] == (interchange if middle else station), node
        assert marker["attrs"]["data-station-id"] == node


def test_a_tick_is_square_to_its_line_bisects_a_bend_and_stands_toward_its_name():
    """On the line with two bends: every station between the ends has a tick
    0.66 of the line's width along the line, reaching from the line's middle
    to 0.66 of its width past its edge, square to the line through the
    station (the direction from the station before to the one after, which
    at a bend is square to the bend's bisector), and on its name's side."""
    width = 7.0
    svg = render(bends(), width=448, style=shaped(station_shape="tick")).svg
    drawn = markers(svg)
    assert [m["shape"] for m in drawn.values()] == ["tick"] * 7
    for (before, *_), (node, *_), (after, *_) in zip(BENDS, BENDS[1:], BENDS[2:]):
        at, points, arcs = drawn[node]["at"], drawn[node]["points"], drawn[node]["arcs"]
        assert len(points) == 4 and not arcs, node
        assert drawn[node]["attrs"]["fill"] == "#2f6fd6" and "stroke" not in drawn[node]["attrs"]
        back = unit(sub(drawn[before]["at"], at))
        on = unit(sub(drawn[after]["at"], at))
        through = unit(sub(on, back))
        along, across = sub(points[1], points[0]), sub(points[2], points[1])
        assert math.hypot(*along) == pytest.approx(TICK_OF * width, abs=0.02), node
        assert math.hypot(*across) == pytest.approx(width / 2 + TICK_OF * width, abs=0.02), node
        assert abs(dot(unit(across), through)) < 0.005, f"{node}'s tick is not square to its line"
        assert abs(dot(unit(along), through)) > 0.999, node
        if abs(dot(back, on)) < 0.99:  # a bend: the tick lies on its bisector
            assert abs(dot(unit(across), unit((back[0] + on[0], back[1] + on[1])))) > 0.999, node
        # Its inner end on the line's middle, its outer end past the edge on the
        # side its name is on.
        out = unit(across)
        assert abs(dot(sub(points[0], at), out)) < 0.02, node
        assert dot(sub(points[2], at), out) > width / 2
        assert dot(sub(label_at(svg, node), at), out) > 1, f"{node}'s tick is not on its name's side"


def test_a_tick_with_no_name_stands_on_the_right_of_its_edge():
    """With the labels off, every tick between the ends is on the right of the
    way its first edge runs, src to dst: here, the line's direction of travel."""
    drawn = markers(render(bends(), width=448, style=shaped(station_shape="tick"),
                           labels=False).svg)
    for (before, *_), (node, *_), (after, *_) in zip(BENDS, BENDS[1:], BENDS[2:]):
        at, points = drawn[node]["at"], drawn[node]["points"]
        travel = unit(sub(unit(sub(drawn[after]["at"], at)), unit(sub(drawn[before]["at"], at))))
        right = (-travel[1], travel[0])     # y runs down the drawing
        assert dot(sub(points[2], at), right) > 0, node


def test_a_line_s_end_is_one_bar_across_both_sides_where_its_cap_ends():
    """TfL's double tab, as one bar 0.66 of the line's width thick reaching
    0.66 of its width past both edges, its outer face at the end of the line's
    round cap, half the line's width past the station."""
    width, tick = 7.0, TICK_OF * 7.0
    ends = []
    for graph in (bends(), fixture()):
        drawn = markers(render(graph, width=448, style=shaped(station_shape="tick")).svg)
        here = [n for n, m in drawn.items() if m["shape"] == "tick"
                and math.hypot(*sub(m["points"][2], m["points"][1])) > width + tick]
        ends += here
        for node in here:
            at, points = drawn[node]["at"], drawn[node]["points"]
            (edge,) = [e for e in graph.edges if node in (e.src, e.dst)]
            other = drawn[edge.dst if edge.src == node else edge.src]["at"]
            outward = unit(sub(at, other))
            side = (-outward[1], outward[0])
            past = [dot(sub(p, at), outward) for p in points]
            across = [dot(sub(p, at), side) for p in points]
            assert min(past) == pytest.approx(width / 2 - tick, abs=0.02), node
            assert max(past) == pytest.approx(width / 2, abs=0.02), node
            assert min(across) == pytest.approx(-(width / 2 + tick), abs=0.02), node
            assert max(across) == pytest.approx(width / 2 + tick, abs=0.02), node
    # The line's two ends, then the fixture's eight: every end of every line.
    assert ends[:2] == ["k0", "k6"]
    assert sorted(ends[2:]) == ["x11y1", "x11y9", "x14y2", "x15y5", "x1y5", "x5y1", "x5y9", "x8y8"]


def test_a_square_is_turned_with_its_line_and_an_interchange_is_rounded():
    """A station's square is 2 station_radius on a side with its first side
    along the line; on a diagonal it is turned 45 degrees, not drawn upright.
    An interchange's is 2 interchange_radius on a side with its corners rounded
    by a quarter of it, in the ring's fill and outline."""
    svg = render(bends(), width=448, style=shaped(station_shape="square")).svg
    drawn = markers(svg)
    for (before, *_), (node, *_), (after, *_) in zip(BENDS, BENDS[1:], BENDS[2:]):
        at, points = drawn[node]["at"], drawn[node]["points"]
        through = unit(sub(unit(sub(drawn[after]["at"], at)), unit(sub(drawn[before]["at"], at))))
        sides = [sub(b, a) for a, b in zip(points, points[1:] + points[:1])]
        assert [round(math.hypot(*s), 1) for s in sides] == [8.4] * 4, node
        assert abs(dot(unit(sides[0]), through)) > 0.999, f"{node}'s square is not turned"
        assert abs(dot(unit(sides[1]), through)) < 0.005, node
    diagonal = drawn["k3"]["points"]
    assert abs(dot(unit(sub(diagonal[1], diagonal[0])), (1.0, 0.0))) == pytest.approx(
        math.sqrt(0.5), abs=0.005)

    fixture_drawn = markers(draw(shaped(interchange_shape="square")))
    for node in ("x5y5", "x11y5"):
        rounded = fixture_drawn[node]
        assert rounded["shape"] == "square" and rounded["tag"] == "path"
        assert rounded["arcs"] == [(3.0, 3.0, 0.0, 0.0, 1.0)] * 4, node
        xs, ys = zip(*rounded["points"])
        assert (max(xs) - min(xs), max(ys) - min(ys)) == pytest.approx((12, 12), abs=0.02)
        attrs = rounded["attrs"]
        assert (attrs["fill"], attrs["stroke"], attrs["stroke-width"]) == (
            "var(--map-station-fill, #ffffff)", "var(--map-station-stroke, #111111)", "2.20")
    assert {fixture_drawn[n]["shape"] for n in fixture_drawn
            if n not in ("x5y5", "x11y5")} == {"circle"}


def test_no_label_overlaps_a_marker_drawn(monkeypatch):
    """The labels keep clear of each tick and square as drawn, not of the
    circle's square: at a wide line with names close in, a tick reaches past
    where the circle's square ends."""
    placed = []
    real = render_module.place

    def spy(*args, **kwargs):
        answer = real(*args, **kwargs)
        placed.extend(answer[0])
        return answer

    monkeypatch.setattr(render_module, "place", spy)
    for graph in (fixture(), bends()):
        for style in (shaped(station_shape="tick", line_width=12, label_offset=0),
                      shaped(station_shape="square", interchange_shape="square",
                             station_radius=6, interchange_radius=6, label_offset=0)):
            placed.clear()
            drawn = markers(render(graph, width=448, style=style).svg)
            assert placed
            outlines = [Quad.of(m["points"][:4] if not m["arcs"] else m["points"])
                        for m in drawn.values() if m["tag"] == "path"]
            assert outlines
            for label in placed:
                for marker in outlines:
                    assert not collide(label.quad, marker), (label.text, style.station_shape)


def test_the_canvas_holds_every_marker():
    """The network's own box reaches past every tick and square by the
    padding, where a tick on a straight run stands out further than the
    line's own reach, and a turned square further than the interchange's."""
    row = LineGraph(nodes={n: Node(id=n, coord=(x, 0.0), station_id=n, station_label=n)
                           for n, x in (("a", 0.0), ("b", 1.0), ("c", 2.0))},
                    edges=[Edge(src=a, dst=b, geometry=[(x, 0.0), (x + 1.0, 0.0)],
                                lines=[Line(id="R", label="R", color="#d6322f")])
                           for a, b, x in (("a", "b", 0.0), ("b", "c", 1.0))])
    slant = LineGraph(nodes={n: Node(id=n, coord=xy, station_id=n, station_label=n)
                             for n, xy in (("p", (0.0, 0.0)), ("q", (1.0, 1.0)))},
                      edges=[Edge(src="p", dst="q", geometry=[(0.0, 0.0), (1.0, 1.0)],
                                  lines=[Line(id="S", label="S", color="#2f6fd6")])])
    for graph, style in ((row, shaped(station_shape="tick")),
                         (slant, shaped(station_shape="square", line_width=1,
                                        station_radius=10, interchange_radius=10))):
        for labels in (True, False):
            svg = render(graph, width=300, style=style, labels=labels).svg
            x, y, w, h = nolabels(svg)
            x0, y0, x1, y1 = x + style.padding, y + style.padding, x + w - style.padding, \
                y + h - style.padding
            corners = [p for m in markers(svg).values() for p in m["points"]]
            assert corners
            for px, py in corners:
                assert x0 - 0.01 <= px <= x1 + 0.01 and y0 - 0.01 <= py <= y1 + 0.01, \
                    f"({px:.2f}, {py:.2f}) is outside the network's box {x0, y0, x1, y1}"


@pytest.mark.parametrize("fields,named", [
    ({"station_shape": "diamond"}, "station_shape"), ({"station_shape": None}, "station_shape"),
    ({"station_shape": "Tick"}, "station_shape"), ({"interchange_shape": "tick"}, "interchange_shape"),
    ({"interchange_shape": "ring"}, "interchange_shape"),
])
def test_a_style_refuses_a_shape_that_is_not_one_when_it_is_made(fields, named):
    """``Style`` itself, for a Python caller the server never sees, and again
    through ``replace``: a tick is not an interchange's shape."""
    with pytest.raises(ValueError, match=f"^{named} must be "):
        Style(**fields)
    with pytest.raises(ValueError, match=f"^{named} must be "):
        dataclasses.replace(Style(themed=True), **fields)


def test_an_interchange_is_never_a_tick_even_from_a_style_changed_after_it_was_made():
    """A style changed after it was made is past ``Style``'s own check, so the
    renderer holds the rule too: an interchange told to be a tick is the ring,
    and a station told to be a shape that is not one is a circle."""
    style = shaped(station_shape="tick")
    style.interchange_shape = "tick"
    drawn = markers(draw(style))
    assert {node: drawn[node]["shape"] for node in ("x5y5", "x11y5")} == \
        {"x5y5": "circle", "x11y5": "circle"}
    assert drawn["x5y5"]["attrs"]["r"] == drawn["x11y5"]["attrs"]["r"] == "6.00"
    assert sum(m["shape"] == "tick" for m in drawn.values()) == 10
    style.station_shape = "hexagon"
    assert {m["shape"] for m in markers(draw(style)).values()} == {"circle"}


def test_where_lines_cross_straight_through_the_first_line_turns_the_square():
    """At x11y5 three lines run straight through: A east-west, D north-south
    and C on the diagonal, each pair of legs as opposite as the others. The
    rounded square turns with A, the first in edge order, however the last
    bits of the diagonal's ends fall: nudged by up to three units in the last
    place, a dot product of -1.0000000000000002 once chose C's 45 degrees."""
    def square_at(graph: LineGraph) -> str:
        return markers(render(graph, width=448, style=shaped(interchange_shape="square")).svg)[
            "x11y5"]["attrs"]["d"]

    def nudge(value: float, steps: int) -> float:
        for _ in range(abs(steps)):
            value = math.nextafter(value, math.inf if steps > 0 else -math.inf)
        return value

    upright = square_at(fixture())
    assert outline(upright)[1][0] == pytest.approx((317.0, 122.0), abs=0.01)  # its top edge
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            graph = fixture()
            for edge in graph.edges:
                if edge.lines[0].label == "C":
                    far = 0 if edge.dst == "x11y5" else 1
                    points = [list(p) for p in edge.geometry]
                    points[far] = [nudge(points[far][0], dx), nudge(points[far][1], dy)]
                    edge.geometry = [tuple(p) for p in points]
            assert square_at(graph) == upright, (dx, dy)


def test_over_a_stored_layout_each_shape_moves_no_stage_and_no_station(tmp_path):
    """Pittsburgh from its stored layout, in circles, in ticks and in squares:
    the four stage files are not touched, each map is drawn from the stored
    layout, and every station's marker is where its circle is. Skips where
    the layout is not stored."""
    stored = _stored("pittsburgh-t")
    day = dt.date(2026, 9, 10)

    def stages() -> dict[str, str]:
        return {stage: hashlib.sha256(path.read_bytes()).hexdigest()
                for stage, path in stored.paths.items()}

    before = stages()
    plain = pipeline.run("pittsburgh-t", layout=stored.id, date=day, out_dir=tmp_path / "plain")
    circles_at = {node: m["at"] for node, m in markers(plain.render.svg).items()}
    assert {m["shape"] for m in markers(plain.render.svg).values()} == {"circle"}
    for station, interchange in (("tick", "circle"), ("square", "square")):
        result = pipeline.run("pittsburgh-t", layout=stored.id, date=day,
                              out_dir=tmp_path / station,
                              style=serve._style({"station_shape": station,
                                                  "interchange_shape": interchange}))
        assert result.layout == plain.layout == stored.id
        drawn = markers(result.render.svg)
        assert {node: m["at"] for node, m in drawn.items()} == circles_at, station
        assert {m["shape"] for m in drawn.values()} == {station, interchange}
        assert tracks(result.render.svg) == tracks(plain.render.svg)
        assert (tmp_path / station / "pittsburgh-t.svg").read_text() == result.render.svg
    assert stages() == before
