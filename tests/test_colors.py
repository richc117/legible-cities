"""Line colours are inputs: a caller's override, else the feed's own, else a default.

The resolution lives in ``render.line_colors`` and happens once per build.
The map's strokes and the page's ``data.lines`` read the same dictionary,
so an uncoloured line looks the same on the map, in the chips, under the
train dots and in the time chart. Pittsburgh's inclines (``MI``, ``DQI``)
are the case that shaped this: no ``route_color`` in the feed, and before
E06 the page had no entry for them and each element fell back on its own.
"""

import datetime as dt
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from schematic import animate, config, loom, pipeline, serve
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph
from schematic.render import Style, check_color, line_colors, render

SNAPSHOTS = Path(__file__).parent / "fixtures" / "colors"
DEFAULT = Style().default_line_color
DAY = dt.date(2026, 9, 10)


def _graph(lines_by_edge: list[list[tuple[str, str | None]]]) -> LineGraph:
    """A chain of stations, one edge per entry, each carrying the lines
    given as (label, route_color as GTFS writes it, or None)."""
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [i * 10.0, 0.0]},
              "properties": {"id": f"n{i}", "station_id": f"S{i}", "station_label": f"Stop {i}"}}
             for i in range(len(lines_by_edge) + 1)]
    for i, lines in enumerate(lines_by_edge):
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [[i * 10.0, 0.0], [(i + 1) * 10.0, 0.0]]},
                      "properties": {"from": f"n{i}", "to": f"n{i + 1}",
                                     "lines": [{"id": label, "label": label,
                                                **({"color": color} if color else {})}
                                               for label, color in lines]}})
    return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})


def strokes(svg: str) -> dict[str, str]:
    return dict(re.findall(r'<g class="line" data-line="([^"]+)" stroke="([^"]+)"', svg))


# ------------------------------------------------------------- resolution

def test_an_override_wins_then_the_feed_then_the_default():
    graph = _graph([[("A", "0072bc"), ("B", None)], [("C", "#ff0000"), ("B", "")]])
    assert line_colors(graph, default=DEFAULT) == {"A": "#0072bc", "B": DEFAULT, "C": "#ff0000"}
    resolved = line_colors(graph, default="#123456",
                           overrides={"A": "#ABCDEF", "Z": "#000000"})
    # Keyed in the order the lines first appear; an override for a line the
    # graph does not carry is ignored, not refused.
    assert list(resolved) == ["A", "B", "C"]
    assert resolved == {"A": "#ABCDEF", "B": "#123456", "C": "#ff0000"}


# "#123456\n": ``$`` also matches before a final newline, so a colour read with
# ``match`` passed it (issue 57); the app's validators refuse it.
@pytest.mark.parametrize("bad", ["red", "123456", "#12345", "#1234567", "#ggg000", "", 0x123456,
                                 None, ["#123456"], "#123456\n", "#abcdef\n"])
def test_a_colour_not_written_rrggbb_is_refused(bad):
    graph = _graph([[("A", None)]])
    with pytest.raises(ValueError, match="'A' must be a colour written #rrggbb"):
        line_colors(graph, default=DEFAULT, overrides={"A": bad})
    with pytest.raises(ValueError, match="default_color must be a colour written #rrggbb"):
        check_color(bad, "default_color")


def test_the_map_and_the_page_read_one_resolution():
    """Every line has a stroke and a page entry, and they are the same string."""
    graph = _graph([[("A", "0072bc"), ("B", None)], [("B", None), ("C", "ff0000")]])
    r = render(graph, colors={"C": "#00ff00"}, labels=False)
    assert strokes(r.svg) == {"A": "#0072bc", "B": DEFAULT, "C": "#00ff00"} == r.colors
    page = animate.build(r, graph, [], DAY)
    assert page.lines == r.colors
    assert page.to_json()["lines"] == r.colors


# ------------------------------------------------- the page's structure
#
# Issue 81. The page around its data was pinned by a hash, which moved with
# every change to page.html, so nearly every commit that touched the snapshot
# did so only to move it. What a client depends on is narrower, and is held here
# instead: the page's top-level sections, the methods window.__present offers,
# and what the page asks of a frame that hosts it -- its scripts and styles
# inline, its images and fonts from data: URIs, and nothing fetched.

# The html element's children, in order, as (tag, id): the head's elements
# (the page is written without <head> and <body>), the header, the stage the
# map is drawn in and an export screenshots, the overlay and the title card a
# capture paints over it, the data, the routing and the script.
SECTIONS = [("meta", None), ("meta", None), ("title", None), ("script", None),
            ("style", None), ("header", None), ("main", "stage"),
            ("div", "present-overlay"), ("div", "present-card"), ("script", "data"),
            ("script", "routing"), ("script", None)]

# The seam and the host policy are the protocol's (engine issue 84): `--schema`
# says them under `page`, and the page is held to what it says, so a method
# added to one and not the other fails with its name.
PAGE = serve.schema()["page"]

# The seam: everything else in the page is closed over inside its script.
# test_the_seam_holds_every_method_the_protocol_and_the_recorder_name holds
# the protocol's prose, bin/_record.js and present.js to this list too.
SEAM = set(PAGE["seam"])

# What a frame must allow the page, as Content-Security-Policy would say it:
# the most a page may need, since one drawn in the system face embeds no font.
NEEDS = set(PAGE["requires"])

# How each requirement is found in a page: from its script's code, its styles
# (stylesheets and style attributes) and the addresses it names. A
# requirement the schema states with no detector here fails
# test_the_schema_requires_only_what_the_page_can_be_seen_to_need rather than
# passing unchecked.
DETECTORS = {
    "script-src 'unsafe-inline'": lambda code, styles, addresses: any(t.strip() for t in code),
    "style-src 'unsafe-inline'": lambda code, styles, addresses: bool(styles),
    "img-src data:": lambda code, styles, addresses: any(a.startswith("data:image/")
                                                         for a in addresses),
    "font-src data:": lambda code, styles, addresses: any(a.startswith("data:font/")
                                                          for a in addresses),
}

# What a page would fetch with, or load code by, as patterns: a space before
# the parenthesis is still a call.
NETWORK = (r"\bfetch\s*\(", r"\bXMLHttpRequest\b", r"\bWebSocket\b", r"\bEventSource\b",
           r"\bsendBeacon\b", r"\bimport\s*\(", r"\bimportScripts\b",
           r"\bnew\s+(?:Shared)?Worker\s*\(")

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
         "source", "track", "wbr"}


class _Outline(HTMLParser):
    """The page's top-level elements, its scripts and styles with their text,
    and every element's attributes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.open: list[str] = []
        self.sections: list[tuple[str, str | None]] = []
        self.scripts: list[tuple[dict, str]] = []
        self.styles: list[str] = []
        self.attrs: list[tuple[str, dict]] = []
        self._text: list[str] | None = None
        self._of: dict = {}

    def handle_starttag(self, tag, attrs):
        given = dict(attrs)
        self.attrs.append((tag, given))
        if self.open == ["html"]:
            self.sections.append((tag, given.get("id")))
        if tag in ("script", "style"):
            self._text, self._of = [], given
        if tag not in _VOID:
            self.open.append(tag)

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._text is not None:
            text = "".join(self._text)
            if tag == "script":
                self.scripts.append((self._of, text))
            else:
                self.styles.append(text)
            self._text = None
        if tag in self.open:
            while self.open.pop() != tag:
                pass

    def handle_data(self, data):
        if self._text is not None:
            self._text.append(data)


def _strip_js(js: str) -> str:
    """``js`` with its comments and string literals blanked, so that neither a
    brace nor a name inside one is read as code."""
    out, i, n = [], 0, len(js)
    while i < n:
        if js.startswith("//", i):
            j = js.find("\n", i)
            j = n if j < 0 else j
        elif js.startswith("/*", i):
            j = js.index("*/", i + 2) + 2
        elif js[i] in "'\"`":
            j = i + 1
            while j < n and js[j] != js[i]:
                j += 2 if js[j] == "\\" else 1
            j += 1
        else:
            out.append(js[i])
            i += 1
            continue
        out.append(" " * (j - i))
        i = j
    return "".join(out)


def _seam_top(script: str) -> str:
    """The top level of ``window.__present``'s object literal, its methods'
    bodies left out, with comments and strings blanked."""
    start = script.index("window.__present = {") + len("window.__present = ")
    code = _strip_js(script[start:])
    depth, top = 0, []
    for ch in code:
        if ch == "{":
            depth += 1
            if depth == 1:
                continue
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        if depth == 1:
            top.append(ch)
    return "".join(top)


def _seam(script: str) -> set[str]:
    """The names ``window.__present``'s object literal defines, at its top level."""
    return set(re.findall(r"(?:^|,)\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*[(:]", _seam_top(script)))


def _seam_arity(script: str) -> tuple[dict[str, int], dict[str, str]]:
    """Each method's count of parameters, as its shorthand definition
    (``name(a, b) {``) lists them, and apart the methods whose list holds
    something other than plain names: a default, a rest or a pattern would
    make the count a judgement, and the page has none of them."""
    arity, odd = {}, {}
    for name, listed in re.findall(r"(?:^|,)\s*(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(([^)]*)\)",
                                   _seam_top(script)):
        params = [p.strip() for p in listed.split(",")]
        if params and params[-1] == "":
            params.pop()                       # a trailing comma, or no parameters
        if all(re.fullmatch(r"[A-Za-z_$][\w$]*", p) for p in params):
            arity[name] = len(params)
        else:
            odd[name] = listed.strip()
    return arity, odd


def _structure(page: str) -> dict:
    """What the page is made of, as far as a client depends on it."""
    outline = _Outline()
    outline.feed(page)
    outline.close()
    code = [text for attrs, text in outline.scripts if attrs.get("type") != "application/json"]
    present = [text for text in code if "window.__present = {" in text]
    seams = [_seam(text) for text in present]
    styles = outline.styles + [a["style"] for _, a in outline.attrs if "style" in a]
    addresses = [a[k] for _, a in outline.attrs for k in ("src", "href", "xlink:href") if k in a]
    addresses += re.findall(r"""url\(\s*["']?([^)"'\s]+)""",
                            "\n".join(styles + [v for _, a in outline.attrs
                                                for v in a.values() if v]))
    needs = {need for need, seen in DETECTORS.items() if seen(code, styles, addresses)}
    arity, odd = _seam_arity(present[0]) if len(present) == 1 else ({}, {})
    return {
        "sections": outline.sections,
        "seam": seams[0] if len(seams) == 1 else seams,
        "arity": arity,
        "odd": odd,
        "needs": needs,
        # An address with a scheme other than data: is one the page would
        # fetch, or send a reader to; a relative one or a fragment is neither.
        "remote": sorted({a for a in addresses if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", a)
                          and not a.startswith("data:")}),
        "network": sorted({pattern for pattern in NETWORK for text in code
                           if re.search(pattern, text)}),
        "loaded": sorted(f"{tag} {a.get('src') or a.get('href')}" for tag, a in outline.attrs
                         if tag == "script" and "src" in a
                         or tag == "link" and "stylesheet" in a.get("rel", "")),
    }


def _held(page: str, *, needs: set[str] = NEEDS) -> None:
    """The page's structure is the one held above, said as a difference."""
    found = _structure(page)
    assert found["sections"] == SECTIONS, found["sections"]
    assert found["seam"] == SEAM, (f"seam gained {sorted(found['seam'] - SEAM)}, "
                                   f"lost {sorted(SEAM - found['seam'])}")
    # Each method takes as many arguments as the schema's maxItems allows.
    assert not found["odd"], f"a parameter list that is not plain names: {found['odd']}"
    assert set(found["arity"]) == found["seam"], (
        f"not defined as methods, so their parameters cannot be counted: "
        f"{sorted(found['seam'] - set(found['arity']))}")
    wrong = sorted((name, count, PAGE["seam"][name]["params"].get("maxItems"))
                   for name, count in found["arity"].items()
                   if count != PAGE["seam"][name]["params"].get("maxItems"))
    assert not wrong, f"(method, the page's parameters, the schema's maxItems) differ: {wrong}"
    assert found["needs"] <= NEEDS, (
        f"the page needs {sorted(found['needs'] - NEEDS)}, which page.requires does not state")
    assert found["needs"] == needs, (f"needs gained {sorted(found['needs'] - needs)}, "
                                     f"lost {sorted(needs - found['needs'])}")
    if not PAGE["fetches"]:
        assert found["network"] == [], f"the page's script fetches with {found['network']}"
        assert found["remote"] == [], f"the page names remote addresses {found['remote']}"
        assert found["loaded"] == [], f"the page loads {found['loaded']}"


def _hand_made_page(tmp_path, style: Style) -> str:
    graph = _graph([[("A", "0072bc"), ("B", None)], [("B", None), ("C", "ff0000")]])
    drawn = render(graph, style=style, title="Testville")
    _, page = animate.write(animate.build(drawn, graph, [], DAY), drawn.svg, tmp_path,
                            stem="testville", name="Testville")
    return page.read_text(encoding="utf-8")


def test_the_page_keeps_its_sections_its_seam_and_its_sinks(tmp_path):
    """Drawn with an embedded label face, the page needs all four sinks."""
    _held(_hand_made_page(tmp_path, Style(themed=True, label_font="inter")))


def test_a_page_drawn_in_the_system_face_needs_no_font(tmp_path):
    _held(_hand_made_page(tmp_path, Style(themed=True)), needs=NEEDS - {"font-src data:"})


def test_the_seam_holds_every_method_the_protocol_and_the_recorder_name():
    """The protocol names the methods a client's capture job drives
    (``--schema``), bin/_record.js is the engine's own capture, and
    present.js is the page's presentation mode, inlined into it and driving
    it through the same seam; all three are held to the seam the page is held
    to, so a method one of them names cannot be renamed on the page alone."""
    protocol = set(re.findall(r"__present\.(\w+)", json.dumps(serve.schema())))
    recorder = (config.REPO_ROOT / "bin" / "_record.js").read_text(encoding="utf-8")
    # The recorder also calls through an alias, `const P = window.__present`.
    called = set(re.findall(r"__present\.(\w+)", recorder)) | set(re.findall(r"\bP\.(\w+)\(",
                                                                              recorder))
    # So does present.js, `var P = window.__present`, and it tests one method
    # before calling it (`P.hasGeo && P.hasGeo()`), so any use counts.
    script = (Path(animate.__file__).parent / "page" / "present.js").read_text(encoding="utf-8")
    presented = set(re.findall(r"__present\.(\w+)", script)) | set(re.findall(r"\bP\.(\w+)",
                                                                               script))
    assert "setTrip" in protocol and "setCapture" in called, "the names are still found"
    assert {"setFrame", "onDraw"} <= presented, "present.js's names are still found"
    assert protocol <= SEAM, sorted(protocol - SEAM)
    assert called <= SEAM, sorted(called - SEAM)
    assert presented <= SEAM, f"present.js calls {sorted(presented - SEAM)}, not on the seam"


def test_every_seam_method_says_its_arguments_as_a_list_by_position():
    """A call's arguments are a tuple: ``minItems`` those it must give,
    ``maxItems`` all of them, which the page's parameters are held to above,
    and a type for each position up to ``maxItems``; a method that takes none
    is an empty list of nulls."""
    wrong = []
    for name, method in PAGE["seam"].items():
        params = method["params"]
        least, most = params.get("minItems", 0), params.get("maxItems")
        typed = len(params["prefixItems"]) if "prefixItems" in params else 0
        if params.get("type") != "array" or not isinstance(most, int):
            wrong.append((name, "not a list with a maxItems"))
        elif not least <= most <= typed:
            wrong.append((name, f"minItems {least}, maxItems {most}, {typed} typed"))
        elif typed == 0 and params.get("items") != {"type": "null"}:
            wrong.append((name, "no arguments, but not a list of nulls"))
        if not re.fullmatch(r"\d+\.\d+\.\d+", method.get("since", "")):
            wrong.append((name, f"since {method.get('since')!r} is not a version"))
    assert not wrong, wrong


def test_the_schema_requires_only_what_the_page_can_be_seen_to_need():
    """Every directive page.requires states is one the structure above knows
    how to find in a page, so none is stated without being checked."""
    assert set(PAGE["requires"]) <= set(DETECTORS), sorted(set(PAGE["requires"]) - set(DETECTORS))
    assert len(PAGE["requires"]) == len(NEEDS), "a directive stated twice"
    assert PAGE["fetches"] in (True, False)


# --------------------------------------------------- over stored layouts

def _stored(key: str) -> pipeline.Layout:
    found = pipeline.stored(key)
    if found is None:
        pytest.skip(f"needs the stored layout for {key}")
    return found


def test_an_override_changes_that_lines_stroke_and_entry_and_nothing_else():
    graph = LineGraph.from_geojson(_stored("la-metro-rail").paths["octi"]).reproject(to_mercator)
    plain = render(graph, style=Style(themed=True))
    changed = render(graph, style=Style(themed=True), colors={"A": "#123456"})
    assert strokes(plain.svg)["A"] == "#0072bc"
    assert strokes(changed.svg) == {**strokes(plain.svg), "A": "#123456"}
    # The one attribute is the whole difference between the two documents.
    assert changed.svg.replace('stroke="#123456"', 'stroke="#0072bc"') == plain.svg
    assert animate.build(changed, graph, [], DAY).lines["A"] == "#123456"


def test_uncoloured_lines_get_the_default_on_the_map_and_in_the_page(tmp_path):
    """Pittsburgh's inclines carry no route_color: the default reaches their
    strokes and their page entries, and a chosen default replaces it in both;
    an override alongside it lands on the line it names."""
    stored = _stored("pittsburgh-t")
    result = pipeline.run("pittsburgh-t", layout=stored.id, date=DAY, out_dir=tmp_path)
    drawn = strokes(result.render.svg)
    assert drawn["MI"] == drawn["DQI"] == DEFAULT
    assert result.animation.lines["MI"] == result.animation.lines["DQI"] == DEFAULT
    assert set(result.animation.lines) == set(drawn) == {"RED", "SLVR", "BLUE", "MI", "DQI"}

    chosen = pipeline.run("pittsburgh-t", layout=stored.id, date=DAY, out_dir=tmp_path,
                          default_color="#123456", colors={"RED": "#000000"})
    drawn = strokes((tmp_path / "pittsburgh-t.svg").read_text())
    assert drawn["MI"] == drawn["DQI"] == "#123456" and drawn["RED"] == "#000000"
    written = json.loads((tmp_path / "pittsburgh-t.positions.json").read_text())["lines"]
    assert written == chosen.animation.lines == {**drawn}
    assert "#888888" not in (tmp_path / "pittsburgh-t.html").read_text()


def test_a_bad_colour_is_refused_before_anything_is_written(tmp_path):
    stored = _stored("pittsburgh-t")
    with pytest.raises(ValueError, match="default_color must be a colour written #rrggbb"):
        pipeline.run("pittsburgh-t", layout=stored.id, date=DAY, out_dir=tmp_path,
                     default_color="123456")
    assert not list(tmp_path.iterdir())


def test_without_overrides_the_page_changes_only_by_the_missing_entries(tmp_path):
    """The snapshot was taken from the same stored layout before E06: the SVG
    and the data apart from ``lines`` hash the same, and ``lines`` gains
    exactly the two lines the feed leaves uncoloured. Over the stored stage
    graph, not a regeneration, because a regeneration also moves geometry (app
    ADR-023). ``data_without_lines`` was re-pinned once, for issue 70: every
    map's trips keep a node's hop, and 6 of Pittsburgh's 8 paths (``d``,
    ``gd`` and ``stops``) step across a node where they cut a chord; the SVG
    and ``lines`` did not move. The page around its data was pinned by a hash
    too, re-pinned whenever ``page.html`` changed on purpose; since issue 81
    it is held by its structure instead, the hand-made page's above."""
    snapshot = json.loads((SNAPSHOTS / "pittsburgh-t.json").read_text())
    stored = _stored("pittsburgh-t")
    if stored.id != snapshot["layout"]:
        pytest.skip("the stored Pittsburgh layout is not the one the snapshot was taken from")
    result = pipeline.run("pittsburgh-t", layout=stored.id, date=dt.date.fromisoformat(snapshot["date"]),
                          out_dir=tmp_path)
    sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
    data = json.loads((tmp_path / "pittsburgh-t.positions.json").read_text())
    html = (tmp_path / "pittsburgh-t.html").read_text()
    assert sha(result.render.svg) == snapshot["svg"]
    _held(html, needs=NEEDS - {"font-src data:"})
    rest = {k: v for k, v in data.items() if k != "lines"}
    assert sha(json.dumps(rest, sort_keys=True, separators=(",", ":"))) == snapshot["data_without_lines"]
    assert data["lines"] == {**snapshot["lines"], "MI": DEFAULT, "DQI": DEFAULT}


def test_a_draw_logs_its_own_stages_and_no_path(tmp_path):
    """E37. A draw from a stored layout says the layout was read, then a line
    for each of its own stages with its time; the write names the files and
    never the folder they went to."""
    stored = _stored("pittsburgh-t")
    heard: list[str] = []
    with loom.cancellable(loom.Job(log=heard.append)):
        pipeline.run("pittsburgh-t", layout=stored.id, date=DAY, out_dir=tmp_path)
    assert heard[0] == f"layout {stored.id[:8]}: read from the store"
    assert [line.split(":", 1)[0] for line in heard[1:]] == [
        "schedule", "render", "animate", "write"]
    assert all(re.search(r" \(\d+\.\d s\)$", line) for line in heard[1:]), heard
    assert heard[-1].startswith(
        "write: pittsburgh-t.svg, pittsburgh-t.html and pittsburgh-t.positions.json (")
    assert not any(str(tmp_path) in line for line in heard), heard
