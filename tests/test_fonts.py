"""The label faces (issue 76): ``Style.label_font`` and what each one draws.

``system`` is the viewer's own stack, measured at ``label_char_width`` of an
em a character, and writes exactly the bytes it always did. ``inter`` and
``atkinson-hyperlegible-next`` ship under ``src/schematic/fonts/<name>/`` as
Latin subsets ``bin/build-fonts`` made from pinned releases: the WOFF2, its
advance table, the face's OFL text and a README. A chosen face is measured
name by name from its own advances and embedded in the SVG.

The tables are held to the WOFF2 files with fontTools, a development
dependency, and those tests skip without it. The drawing tests use the
invented twelve-station network the theme pictures are made from, so they run
anywhere; the tests over the stored layouts skip without them, and the one
over New York prints the names placed in each face under ``-s``. The last two
open that network's page in the full Chromium, as tests/test_site.py does, and
skip without it: the rows' bold names stay on the system stack, and the
page's ``settle()`` answers once the face has loaded.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import itertools
import json
import re
import xml.etree.ElementTree as ET

import pytest
from pylsp_jsonrpc.exceptions import JsonRpcInvalidParams
from test_colors import _stored
from test_serve import DATE, KEY, NO_LAYOUT, SCHEMA, Client, check, invalid
from test_site import BROWSER, _run, _trip_page, needs_browser
from test_style import (BOTH, PRESET_NUMBERS, box, circles, line_widths, nolabels, numbers,
                        stand_in_pipeline)

from schematic import feeds, pipeline, serve
from schematic.crs import to_mercator
from schematic.labels import Quad, Station, collide, em_measure, label_quad, place, text_width
from schematic.linegraph import LineGraph
from schematic.names import display_name
from schematic.render import FONTS_DIR, LABEL_FONTS, Style, label_face, label_measure, render
from schematic.theme_thumbnails import fixture

# What the issue decided, written out here and not read from the engine, so a
# name, a family or a range changed in the package alone is a failure of the
# tests and not a new truth for them.
NAMES = ("system", "inter", "atkinson-hyperlegible-next")
FAMILIES = {"inter": "Inter", "atkinson-hyperlegible-next": "Atkinson Hyperlegible Next"}
BUNDLED = tuple(FAMILIES)
RANGES = [[0x0020, 0x007E], [0x00A0, 0x017F], [0x2010, 0x2026]]
SYSTEM = "Helvetica Neue, Helvetica, Arial, sans-serif"
FILES = {"regular.woff2", "advances.json", "OFL.txt", "README.md"}
UNKNOWN = "style.label_font must be system, inter or atkinson-hyperlegible-next"

# The invented network's SVG at width 448 before issue 76, themed as the
# server draws it and plain as a caller of ``render`` does: the system face
# must keep writing these bytes.
DEFAULT_THEMED = "64fb020f643d2d0cdb887569387fe19b8b20e0acd2ccff2897c0702061ca41dc"
DEFAULT_PLAIN = "8e06fe5d4aa286a033b8f4c3882267bedc906d040929981d2c1a305f376a2900"

FONT_FACE = re.compile(r'<style>@font-face\{font-family:"([^"]+)";'
                       r'src:url\(data:font/woff2;base64,([A-Za-z0-9+/=]+)\) '
                       r'format\("woff2"\)\}</style>')


@pytest.fixture
def client():
    c = Client()
    yield c
    c.endpoint.close()


def draw(style: Style) -> str:
    """The invented network at the width its own pictures use."""
    return render(fixture(), width=448, style=style).svg


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def table(name: str) -> dict:
    return json.loads((FONTS_DIR / name / "advances.json").read_text(encoding="utf-8"))


def font_family(svg: str) -> str:
    found = re.match(r'<svg [^>]*font-family="([^"]+)">', svg)
    assert found, "no font-family on the drawing"
    return found.group(1)


def woff2(name: str):
    """The committed WOFF2 as fontTools reads it; skips without fontTools or
    the brotli it decodes WOFF2 with."""
    ttlib = pytest.importorskip("fontTools.ttLib")
    pytest.importorskip("brotli")
    return ttlib.TTFont(FONTS_DIR / name / "regular.woff2")


# ------------------------------------------------------------- the names

def test_the_names_are_the_three_the_issue_decided_and_the_schema_agrees():
    assert LABEL_FONTS == NAMES
    assert Style().label_font == "system"
    prop = SCHEMA["$defs"]["MapStyle"]["properties"]["label_font"]
    assert prop["enum"] == list(NAMES)
    assert "system when omitted" in prop["description"]
    assert "required" not in SCHEMA["$defs"]["MapStyle"]


def test_a_name_the_engine_does_not_ship_is_refused_where_it_is_drawn():
    with pytest.raises(ValueError, match="label_font must be one of system, inter, "
                                         "atkinson-hyperlegible-next, not 'helvetica'"):
        render(fixture(), width=448, style=Style(label_font="helvetica"))
    assert label_face("system") is None


# ------------------------------------------------------------- the files

@pytest.mark.parametrize("name", BUNDLED)
def test_each_face_ships_its_font_its_table_its_licence_and_a_readme(name):
    folder = FONTS_DIR / name
    # A name with a leading dot is a file system's own, such as Finder's
    # .DS_Store, and never something the build wrote.
    assert {path.name for path in folder.iterdir() if not path.name.startswith(".")} == FILES
    licence = (folder / "OFL.txt").read_text(encoding="utf-8")
    assert "SIL OPEN FONT LICENSE Version 1.1" in licence
    # A Reserved Font Name is declared before the licence text begins; a
    # subset could not keep its family name under one.
    assert "reserved font name" not in licence.split("This Font Software is licensed")[0].lower()
    readme = (folder / "README.md").read_text(encoding="utf-8")
    assert "bin/build-fonts" in readme and "sha256" in readme
    assert "U+0020-U+007E, U+00A0-U+017F, U+2010-U+2026" in readme
    got = table(name)
    assert got["family"] == FAMILIES[name]
    assert got["ranges"] == RANGES
    assert f"{got['family']} {got['release']}" in readme


@pytest.mark.parametrize("name", BUNDLED)
def test_the_table_is_the_woff2s_own_advances(name):
    """Read back from the committed font: every code point it maps, the
    advance of the glyph it maps to, the em, the family and the average. The
    subset maps nothing outside the ranges, is weight 400, and has no layout
    tables or hinting left, so a name is drawn as wide as its advances sum."""
    font = woff2(name)
    got = table(name)
    cmap = font.getBestCmap()
    advances = {str(cp): font["hmtx"][glyph][0] for cp, glyph in sorted(cmap.items())}
    assert got["advances"] == advances
    assert got["unitsPerEm"] == font["head"].unitsPerEm
    assert got["family"] == font["name"].getDebugName(1)
    assert got["average"] == round(sum(advances.values()) / len(advances), 2)
    inside = {cp for low, high in RANGES for cp in range(low, high + 1)}
    assert set(cmap) <= inside
    assert set(range(0x20, 0x7F)) <= set(cmap), "the whole of printable ASCII"
    assert font["OS/2"].usWeightClass == 400
    assert not {"GSUB", "GPOS", "GDEF", "kern", "fpgm", "prep", "cvt "} & set(font.keys())


def test_every_character_of_every_stored_station_name_is_in_both_faces():
    """Over every layout stored for a preset of the registry, each name as the
    map draws it: in each face's subset, or named in its README among the
    code points it lacks, which the browser draws in the next face of the
    stack. Only the registry's presets, so a person's own feeds do not decide
    it, and read with ``stored_layouts``, which never writes, where
    ``stored`` may migrate an old layout into its folder. Skips where none is
    stored."""
    names: set[str] = set()
    layouts = 0
    for key in feeds.FEEDS:
        for stored in pipeline.stored_layouts(key):
            layouts += 1
            graph = LineGraph.from_geojson(stored.paths["octi"])
            names |= {display_name(n.station_label) for n in graph.stations if n.station_label}
    if not layouts:
        pytest.skip("needs the stored layouts")
    characters = {ch for name in names for ch in name}
    print(f"\n{len(names)} station names in {layouts} stored layouts, "
          f"{len(characters)} characters")
    for face in BUNDLED:
        mapped = {int(cp) for cp in table(face)["advances"]}
        listed = (FONTS_DIR / face / "README.md").read_text(encoding="utf-8")
        missing = sorted(ch for ch in characters
                         if ord(ch) not in mapped and f"U+{ord(ch):04X}" not in listed)
        assert not missing, f"{face} lacks {missing}, and its README does not say so"


# ------------------------------------------------------------- the drawing

def test_the_default_svg_is_unchanged_with_label_font_omitted_or_system():
    themed = draw(Style(themed=True))
    assert sha(themed) == DEFAULT_THEMED
    assert draw(Style(themed=True, label_font="system")) == themed
    assert draw(serve._style({"label_font": "system"})) == themed
    assert sha(draw(Style())) == sha(draw(Style(label_font="system"))) == DEFAULT_PLAIN
    assert serve._style({"label_font": "system"}) == serve._style({}) == Style(themed=True)
    assert font_family(themed) == SYSTEM
    assert "<style" not in themed and "@font-face" not in themed


@pytest.mark.parametrize("name", BUNDLED)
def test_a_chosen_face_embeds_one_font_face_with_the_committed_bytes(name):
    svg = draw(Style(themed=True, label_font=name))
    assert svg.count("<style") == 1 and svg.count("@font-face") == 1
    found = FONT_FACE.search(svg)
    assert found, "no @font-face of a woff2 data URI"
    family, data = found.groups()
    assert family == FAMILIES[name]
    shipped = (FONTS_DIR / name / "regular.woff2").read_bytes()
    assert base64.b64decode(data, validate=True) == shipped
    # Named first, the system stack behind it, and in place before anything
    # is drawn.
    assert font_family(svg) == f"{FAMILIES[name]}, {SYSTEM}"
    assert svg.index("<style>") < svg.index('<rect id="backdrop"')
    # A face is bytes the engine ships, so two drawings are the same bytes.
    assert draw(Style(themed=True, label_font=name)) == svg


@pytest.mark.parametrize("name", BUNDLED)
def test_a_face_re_places_the_labels_and_leaves_the_network_where_it_was(name):
    plain, faced = draw(Style(themed=True)), draw(Style(themed=True, label_font=name))
    assert nolabels(faced) == nolabels(plain)
    assert circles(faced) == circles(plain)
    assert box(faced) != box(plain), "the names are measured, so the labelled box moves"


# ------------------------------------------------------------- the measure

def test_the_placer_sums_each_names_own_advances():
    """Inter's I and l are narrow and its W wide: three of each are three
    of each, not three averages. A code point outside the subset counts as
    the face's average; the system face is 0.56 of the size a character."""
    face, size = label_face("inter"), 11.0
    got = table("inter")
    upem, adv = got["unitsPerEm"], got["advances"]

    def summed(text: str) -> float:
        return sum(adv[str(ord(ch))] for ch in text) * size / upem

    measure = label_measure(Style(label_font="inter"))
    for text in ("Ill", "WWW", "Fulton St", "Coney Island-Stillwell Av"):
        assert text_width(text, size, measure) == pytest.approx(summed(text), abs=1e-9), text
    assert text_width("Ill", size, measure) < text_width("WWW", size, measure) / 2
    assert text_width("Ж", size, measure) == pytest.approx(got["average"] * size / upem)
    assert face.measure()("WWW", size) == text_width("WWW", size, measure)

    system = label_measure(Style())
    assert text_width("Ill", size, system) == text_width("WWW", size, system) == 3 * size * 0.56
    assert text_width("Ill", size, em_measure(0.5)) == 3 * size * 0.5


def test_the_placer_takes_a_measure_or_a_char_width_and_not_both():
    """A char_width is the em estimate it always was, placed exactly as the
    same estimate given as a measure; both, or neither, is refused, so a
    caller cannot think a face was measured when it was not."""
    stations = [Station(text=f"Stop {i}", x=i * 40.0, y=0.0, key=str(i)) for i in range(6)]
    common = {"size": 11, "offset": 9, "marker_radius": 6}

    def drawn(placed):
        return [(p.text, p.x, p.y, p.anchor, p.rotate) for p in placed[0]]

    assert drawn(place(stations, [], char_width=0.56, **common)) == \
        drawn(place(stations, [], measure=em_measure(0.56), **common))
    for wrong in ({}, {"char_width": 0.56, "measure": em_measure(0.56)}):
        with pytest.raises(TypeError, match="a measure or a char_width"):
            place(stations, [], **wrong, **common)


# ------------------------------------------------------------- the wire

@pytest.mark.parametrize("name", NAMES)
def test_style_takes_each_name_and_builds_it_themed(name):
    assert serve._style({"label_font": name}) == Style(themed=True, label_font=name)
    style = serve._style({"label_font": name, "label_size": 14})
    assert (style.label_font, style.label_size, style.themed) == (name, 14, True)
    check({"key": KEY, "layout": NO_LAYOUT, "date": DATE, "style": {"label_font": name}},
          "MapBuildParams")


@pytest.mark.parametrize("name", ["Inter", "inter ", "helvetica", "", "default", None, 3, True,
                                  ["inter"], {"inter": 1}])
def test_an_unknown_face_is_refused_naming_the_three(name):
    with pytest.raises(JsonRpcInvalidParams) as refused:
        serve._style({"label_font": name})
    assert refused.value.data["kind"] == "params"
    assert refused.value.data["hint"] == UNKNOWN
    assert invalid({"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                    "style": {"label_font": name}}, "MapBuildParams")


@pytest.mark.parametrize("preset", list(PRESET_NUMBERS))
@pytest.mark.parametrize("name", BUNDLED)
def test_a_preset_beside_a_face_draws_the_presets_numbers_in_that_face(preset, name):
    """The face is not one of the eight numbers a preset resolves to, so the
    two go together: the preset's numbers drawn and measured in the face, the
    same bytes as those numbers sent field by field with it. A preset alone
    still draws in the system face, and beside one of its numbers as well as
    the face is still refused."""
    style = serve._style({"preset": preset, "label_font": name})
    assert style == Style(themed=True, label_font=name, **numbers(preset))
    check({"key": KEY, "layout": NO_LAYOUT, "date": DATE,
           "style": {"preset": preset, "label_font": name}}, "MapBuildParams")
    svg = draw(style)
    assert svg == draw(serve._style({**numbers(preset), "label_font": name}))
    assert font_family(svg) == f"{FAMILIES[name]}, {SYSTEM}"
    assert FONT_FACE.search(svg).group(1) == FAMILIES[name]
    assert set(line_widths(svg)) == {f"{PRESET_NUMBERS[preset][0]:.2f}"}
    alone = serve._style({"preset": preset})
    assert alone.label_font == "system" and draw(alone) != svg
    with pytest.raises(JsonRpcInvalidParams) as refused:
        serve._style({"preset": preset, "label_font": name, "padding": 24})
    assert refused.value.data["hint"] == BOTH


def test_map_build_hands_the_face_to_the_pipeline_and_refuses_an_unknown_one(
        client, tmp_path, monkeypatch):
    asked = stand_in_pipeline(monkeypatch, tmp_path)
    for i, name in enumerate(NAMES):
        sent = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                                         "out": f"f{i}", "style": {"label_font": name}})
        assert "result" in sent, sent
        assert asked[-1]["style"] == Style(themed=True, label_font=name)
    sent = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE, "out": "fp",
                                     "style": {"preset": "beck", "label_font": "inter"}})
    assert "result" in sent, sent
    assert asked[-1]["style"] == Style(themed=True, label_font="inter", **numbers("beck"))
    error = client.call("map.build", {"key": KEY, "layout": NO_LAYOUT, "date": DATE,
                                      "style": {"label_font": "comic-sans"}})["error"]
    assert error["code"] == -32602 and error["data"]["kind"] == "params"
    assert error["data"]["hint"] == UNKNOWN
    assert len(asked) == len(NAMES) + 1, "a refused request never reaches the pipeline"


# ------------------------------------------------------------- stored layouts

def test_over_a_stored_layout_the_face_reaches_the_svg_and_the_page(tmp_path):
    """Pittsburgh drawn in Inter: the SVG written to disk is the one answered,
    and the page that inlines it carries its one @font-face, so the names the
    page draws inside the SVG are drawn in the face they were measured in."""
    stored = _stored("pittsburgh-t")
    result = pipeline.run("pittsburgh-t", layout=stored.id, date=dt.date(2026, 9, 10),
                          out_dir=tmp_path, style=Style(themed=True, label_font="inter"))
    svg = (tmp_path / "pittsburgh-t.svg").read_text(encoding="utf-8")
    page = (tmp_path / "pittsburgh-t.html").read_text(encoding="utf-8")
    assert svg == result.render.svg
    assert svg.count("@font-face") == page.count("@font-face") == 1
    assert FONT_FACE.search(page).group(0) == FONT_FACE.search(svg).group(0)
    assert f'font-family="Inter, {SYSTEM}"' in page


def _labels(svg: str) -> list[tuple[float, float, str, float, str]]:
    """Each drawn name: x, y, its anchor, its rotation and its text."""
    ns = "{http://www.w3.org/2000/svg}"
    group = next(g for g in ET.fromstring(svg).iter(f"{ns}g") if g.get("id") == "labels")
    out = []
    for text in group.iter(f"{ns}text"):
        turn = re.match(r"rotate\(([-\d.]+) ", text.get("transform", ""))
        out.append((float(text.get("x")), float(text.get("y")), text.get("text-anchor"),
                    float(turn.group(1)) if turn else 0.0, text.text or ""))
    return out


def test_on_new_york_no_label_overlaps_a_label_or_a_marker_in_any_bundled_face():
    """The issue's criterion. New York from its stored layout, drawn in each
    face as ``map.build`` draws it; every placed name is measured again from
    the committed font file, not the table, at ``label_size``, and its box may
    touch no other name's and no station's square of the interchange radius,
    the square the placer keeps clear. Prints the names placed per face."""
    stored = _stored("nyc-subway")
    graph = LineGraph.from_geojson(stored.paths["octi"]).reproject(to_mercator)
    placed = {}
    for name in NAMES:
        style = Style(themed=True, label_font=name)
        result = render(graph, style=style)
        labels = _labels(result.svg)
        placed[name] = len(labels)
        if name == "system":
            continue  # no file to measure it from: the viewer's fonts answer
        font = woff2(name)
        cmap, hmtx, upem = font.getBestCmap(), font["hmtx"], font["head"].unitsPerEm
        boxes = []
        for x, y, anchor, turn, text in labels:
            width = sum(hmtx[cmap[ord(ch)]][0] for ch in text) * style.label_size / upem
            boxes.append((text, label_quad(x, y, width, style.label_size, anchor, turn)))
        r = style.interchange_radius
        markers = [Quad.rect(x - r, y - r, x + r, y + r)
                   for x, y in (result.node_xy[n.id] for n in graph.stations)]
        crossed = [(a, b) for (a, qa), (b, qb) in itertools.combinations(boxes, 2)
                   if collide(qa, qb)]
        assert not crossed, f"{name}: names overlap: {crossed[:5]}"
        covered = [text for text, quad in boxes if any(collide(quad, m) for m in markers)]
        assert not covered, f"{name}: names over a station: {covered[:5]}"
    print("\nNew York, names placed: "
          + ", ".join(f"{name} {count}" for name, count in placed.items()))


# ------------------------------------------------------------- in the page
#
# The page inlines the SVG, so its own names and the time chart's text take the
# face from the SVG's font-family. The rows' names are bold and the face is its
# regular weight alone, so they keep the system stack. The face is a data URI
# the browser decodes after the page's first pass: settle() answers once it is
# in, and fits the showing lines' box again if it was measured before.

def _pages(tmp_path) -> dict[str, str]:
    return {face: _trip_page(tmp_path / face, "Twelve", fixture(),
                             Style(themed=True, label_font=face))
            for face in ("inter", "system")}


FACES_ON_THE_PAGE = BROWSER + r"""
main(async browser => {
  const out = { problems: [], pages: {} };
  for (const [face, url] of Object.entries(job.urls)) {
    const page = await browser.newPage();
    page.on("pageerror", e => out.problems.push(e.message));
    await page.goto(url, { waitUntil: "load" });
    await ready(page);
    await page.evaluate(() => window.__present.settle());
    // The fonts the browser drew each element's text with, which a computed
    // font-family alone does not say: a faked bold is the face, not a system font.
    const cdp = await page.context().newCDPSession(page);
    await cdp.send("DOM.enable");
    await cdp.send("CSS.enable");
    const { root } = await cdp.send("DOM.getDocument", { depth: -1 });
    const seen = {};
    const what = { rows: ".rowname", names: "#linear-labels text", map: "#labels text" };
    for (const [name, selector] of Object.entries(what)) {
      const { nodeId } = await cdp.send("DOM.querySelector", { nodeId: root.nodeId, selector });
      const { fonts } = await cdp.send("CSS.getPlatformFontsForNode", { nodeId });
      seen[name] = {
        family: await page.evaluate(s => getComputedStyle(document.querySelector(s)).fontFamily,
                                    selector),
        fonts: fonts.map(f => ({ name: f.familyName, custom: f.isCustomFont })),
      };
    }
    out.pages[face] = seen;
    await page.close();
  }
  return out;
}).catch(fail);
"""


@needs_browser
def test_the_rows_names_stay_on_the_system_stack_and_the_maps_names_take_the_face(tmp_path):
    seen = _run(FACES_ON_THE_PAGE, {"urls": _pages(tmp_path)})
    assert not seen["problems"], seen["problems"]
    stack = '"Helvetica Neue", Helvetica, Arial, sans-serif'
    for face, page in seen["pages"].items():
        assert page["rows"]["family"] == stack, face
        assert page["rows"]["fonts"] and not any(f["custom"] for f in page["rows"]["fonts"]), \
            f"{face}: the rows' names are drawn in {page['rows']['fonts']}"
    inter = seen["pages"]["inter"]
    for what in ("names", "map"):
        assert inter[what]["family"] == f"Inter, {stack}", what
        assert inter[what]["fonts"] == [{"name": "Inter", "custom": True}], what
    system = seen["pages"]["system"]
    for what in ("names", "map"):
        assert system[what]["family"] == stack, what
        assert not any(f["custom"] for f in system[what]["fonts"]), what


SETTLE = BROWSER + r"""
// Run in the page before its own script: when the seam is assigned, which is
// before the page's first frame and while the map's face is still loading.
function settleAtTheSeam() {
  Object.defineProperty(window, "__present", { configurable: true, get() { return undefined; },
    set(seam) {
      Object.defineProperty(window, "__present", { value: seam, writable: true,
                                                   configurable: true });
      const loading = Array.from(document.fonts, f => f.status);
      const answer = seam.settle();
      const promised = !!answer && typeof answer.then === "function";
      window.__settled = Promise.resolve(answer).then(() => ({
        loading, promised, status: document.fonts.status,
        faces: Array.from(document.fonts, f => f.status) }));
    } });
}
function fitAtTheSeam() {
  Object.defineProperty(window, "__present", { configurable: true, get() { return undefined; },
    set(seam) {
      Object.defineProperty(window, "__present", { value: seam, writable: true,
                                                   configurable: true });
      seam.setRoutes(["A"]);
    } });
}
main(async browser => {
  const out = { problems: [], pages: {} };
  for (const [face, url] of Object.entries(job.urls)) {
    const seen = {};
    let page = await browser.newPage();
    page.on("pageerror", e => out.problems.push(e.message));
    await page.addInitScript(settleAtTheSeam);
    await page.goto(url, { waitUntil: "load" });
    seen.settled = await page.evaluate(() => window.__settled);
    await page.close();

    // One line fitted while the face loads, as an address's lines= is; then a
    // settle once it has loaded, as a driver asks after its own wait.
    page = await browser.newPage();
    page.on("pageerror", e => out.problems.push(e.message));
    await page.addInitScript(fitAtTheSeam);
    await page.goto(url, { waitUntil: "load" });
    await ready(page);
    await page.evaluate(() => document.fonts.ready);
    seen.fit = await page.evaluate(async () => {
      const P = window.__present, early = P.state().box;
      const answer = P.settle();
      const sync = P.state().box;
      await answer;
      P.setRoutes(["A"]);
      return { early, sync, fresh: P.state().box };
    });
    await page.close();
    out.pages[face] = seen;
  }
  return out;
}).catch(fail);
"""


@needs_browser
def test_settle_answers_once_the_face_has_loaded_and_fits_what_was_measured_before(tmp_path):
    """Both drivers get the face by construction: a settle asked while it
    loads resolves only once it is in, and a box fitted to one line in the
    fallback face is fitted again at the settle, before it returns. The system
    face has nothing to wait for and nothing to fit again."""
    seen = _run(SETTLE, {"urls": _pages(tmp_path)})
    assert not seen["problems"], seen["problems"]
    inter, system = seen["pages"]["inter"], seen["pages"]["system"]

    assert inter["settled"]["loading"] != ["loaded"], "the face had loaded before the seam"
    assert inter["settled"]["promised"] and system["settled"]["promised"]
    assert inter["settled"]["status"] == "loaded" and inter["settled"]["faces"] == ["loaded"]
    assert system["settled"]["status"] == "loaded" and system["settled"]["faces"] == []

    fit = inter["fit"]
    assert fit["early"] != fit["fresh"], "the box was not fitted in the fallback face"
    assert fit["sync"] == fit["fresh"]
    fit = system["fit"]
    assert fit["early"] == fit["sync"] == fit["fresh"]
