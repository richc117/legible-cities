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

import datetime as dt
import hashlib
import re
from types import SimpleNamespace

import pytest
from pylsp_jsonrpc.exceptions import JsonRpcInvalidParams
from test_colors import _graph, _stored
from test_serve import DATE, KEY, NO_LAYOUT, SCHEMA, Client, check, invalid

from schematic import animate, config, diagnostics, pipeline, serve
from schematic.render import PRESETS, STYLE_RANGES, Style, preset_style, render
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
    assert set(props) == set(RANGES) | set(COLORS) | {"preset", "label_font"}
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
        assert PRESETS[name] == dict(zip(STYLE_RANGES, numbers)), name
        # All eight and nothing else: no colour, which the theme owns.
        assert list(PRESETS[name]) == list(STYLE_RANGES), name


def test_every_preset_value_is_inside_its_range_with_the_interchange_above_the_station():
    for name, numbers in PRESETS.items():
        for field, value in numbers.items():
            low, high = RANGES[field]
            assert low <= value <= high, f"{name}.{field} is {value}, outside {low} to {high}"
        assert numbers["interchange_radius"] > numbers["station_radius"], name


def test_no_preset_equals_the_default_or_another():
    default = {field: getattr(Style(), field) for field in STYLE_RANGES}
    for name, numbers in PRESETS.items():
        assert numbers != default, f"{name} is the default style"
    named = list(PRESETS.items())
    for i, (one, first) in enumerate(named):
        for other, second in named[i + 1:]:
            assert first != second, f"{one} and {other} are the same style"


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
    """The preset's numbers as the issue wrote them, never read from the table."""
    return dict(zip(RANGES, PRESET_NUMBERS[name]))


def test_the_schema_knows_the_presets_by_name_and_by_their_numbers():
    defs = SCHEMA["$defs"]
    assert defs["MapStyle"]["properties"]["preset"]["enum"] == list(PRESETS)
    assert defs["StylePreset"]["properties"]["name"]["enum"] == list(PRESETS)
    assert SCHEMA["methods"]["style.presets"] == {
        "params": {"$ref": "#/$defs/NoParams"}, "result": {"$ref": "#/$defs/StylePresets"}}
    assert defs["StylePresets"]["properties"]["presets"]["items"] == {"$ref": "#/$defs/StylePreset"}
    # The eight, complete and closed, inside the bounds MapStyle sends them with.
    style = defs["StylePreset"]["properties"]["style"]
    assert style["additionalProperties"] is False
    assert list(style["properties"]) == list(RANGES) == style["required"]
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
