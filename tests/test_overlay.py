"""A caption, the clock's corner, and the safe zones as data (issue 40).

An export can carry a caption, which the page draws as text under the title,
and put the clock in any of four corners. The platforms' safe zones are a
dated table in ``export.py``: the plan writes a row's fractions onto the
page's address, the page lays its frame out below the top zone, and under
``safe=1`` draws a box for each fraction it was given, so ``present.js``
holds no zone number of its own (app ADR-052).

The rule that holds it together: an address that asks for neither a caption
nor a corner, on a preset without safe zones, is the one it always was.

All pure but the last test, which drives the page in the full Chromium.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import pytest

from schematic import animate, config, export
from schematic.linegraph import LineGraph
from schematic.render import render
from schematic.schedule import Call, Trip

PRESENT_JS = Path(export.__file__).parent / "page" / "present.js"
PLAYWRIGHT = config.REPO_ROOT / "site" / "node_modules" / "playwright"

# As tests/test_determinism.py has it, without the built page: this file
# writes its own.
needs_browser = pytest.mark.skipif(
    not (PLAYWRIGHT.exists() and shutil.which("node")),
    reason="needs site/node_modules/playwright")

KEY = "la-metro-rail"
PAGE = "app://local/p/la-metro-rail.html"
DATE = "2026-09-10"

# What url_for wrote before issue 40 (at 1015d8e) for this page and day.
TODAY = {
    "instagram-post": (
        "app://local/p/la-metro-rail.html?present=1&view=map&labels=1&title=1&clock=0"
        "&theme=dark&frame=1080%3A1350&frametop=0.46&city=Los+Angeles&network=Metro+Rail"
        "&date=Thursday+10+September+2026"),
    "linkedin-video": (
        "app://local/p/la-metro-rail.html?present=1&view=map&labels=1&title=1&clock=1"
        "&theme=dark&frame=1200%3A1200&frametop=0.46&city=Los+Angeles&network=Metro+Rail"
        "&date=Thursday+10+September+2026"),
}
TODAY_KEYS = ["present", "view", "labels", "title", "clock", "theme", "frame", "frametop",
              "city", "network", "date"]
ZONE_KEYS = ("ztop", "zbottom", "zside", "zrail", "zrailtop")
NEW_KEYS = {"caption", "corner", *ZONE_KEYS}

SENTENCE = "Trains every few minutes from first light, and long gaps between them after ten."


def _query(url: str) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(url).query))


# ------------------------------------------------------------------ the table


def test_the_two_rows_are_the_numbers_measured():
    """Meta's templates as traced on 2 Oct 2026, as fractions of 1080x1920:
    Reels 270, 672 and 64 px and a 227 px rail; Stories 270 and 385 px."""
    assert sorted(export.SAFE_ZONES) == ["instagram-reels", "instagram-stories"]
    reels = export.SAFE_ZONES["instagram-reels"]
    assert (reels.top, reels.bottom, reels.side, reels.rail_width, reels.rail_top) == (
        0.14, 0.35, 0.06, 0.21, 0.60)
    assert reels.measured == dt.date(2026, 10, 2)
    stories = export.SAFE_ZONES["instagram-stories"]
    assert (stories.top, stories.bottom) == (0.14, 0.20)
    assert stories.side is None and stories.rail_width is None and stories.rail_top is None
    assert stories.measured == dt.date(2026, 10, 2)
    assert all(row.source for row in export.SAFE_ZONES.values())


def test_only_the_reel_and_the_story_have_a_row():
    """``instagram-reel-gif`` keeps none: it had no safe zones before, and it
    is the app's determinism fixture, whose frames must not move."""
    for name, preset in export.PRESETS.items():
        assert preset.zones == "" or preset.zones in export.SAFE_ZONES, name
        assert preset.safe_zones == bool(preset.zones), name
    assert [n for n, p in export.PRESETS.items() if p.zones] == [
        "instagram-story", "instagram-reel"]


def test_no_tiktok_row_and_the_presets_platforms_are_five():
    """Issue 40, 6 Oct 2026: TikTok's numbers wait for a preset that asks."""
    assert not any("tiktok" in name.lower() for name in export.SAFE_ZONES)
    assert {p.platform for p in export.PRESETS.values()} == {
        "Instagram", "LinkedIn", "Bluesky", "X", "Portfolio"}


def test_the_presets_table_answers_safe_zones_as_it_did():
    """``export.presets`` is unchanged: the same booleans, in order, as at
    the base commit."""
    assert [p["safe_zones"] for p in export.preset_table()] == [
        False, False, True, False, False, False, False, True,
        False, False, False, False, False, False, False, False]


# ---------------------------------------------------------------- the address


@pytest.mark.parametrize("name", list(TODAY))
def test_an_address_that_asks_for_nothing_new_is_todays(name):
    assert export.url_for(KEY, export.PRESETS[name], page=PAGE, date=DATE) == TODAY[name]
    assert export.plan(KEY, name, page=PAGE, date=DATE).url == TODAY[name]


def test_no_preset_without_zones_gains_a_parameter():
    """Every view of every preset without a row has today's keys, in today's
    order, and none of the seven new ones."""
    for name, preset in export.PRESETS.items():
        if preset.kind == "vector" or preset.zones:
            continue
        for view in export.VIEWS:
            url = export.url_for(KEY, preset, view=view, page=PAGE, date=DATE)
            keys = [k for k, _ in parse_qsl(urlsplit(url).query)]
            assert keys == TODAY_KEYS, f"{name}/{view}"
        planned = export.plan(KEY, name, page=PAGE, date=DATE).url
        assert [k for k, _ in parse_qsl(urlsplit(planned).query)] == TODAY_KEYS, name
        assert not NEW_KEYS & set(_query(planned)), name


def test_the_new_parameters_come_after_every_old_one():
    url = export.url_for(KEY, export.PRESETS["instagram-reel"], page=PAGE, date=DATE,
                         safe=True, caption="Hi", clock_corner="top-right",
                         zones=export.SAFE_ZONES["instagram-reels"])
    keys = [k for k, _ in parse_qsl(urlsplit(url).query)]
    assert keys == TODAY_KEYS + ["safe", "caption", "corner", *ZONE_KEYS]
    with pytest.raises(ValueError, match="clock_corner is one of"):
        export.url_for(KEY, export.PRESETS["linkedin-video"], clock_corner="middle")


# ------------------------------------------------------------------- the plan


def test_a_caption_out_of_bounds_is_refused_with_the_bound():
    with pytest.raises(ValueError, match=r"^A caption is 1 to 80 characters on one line; "
                                         r"this one is 81\.$"):
        export.plan(KEY, "instagram-reel", caption="x" * 81)
    with pytest.raises(ValueError, match=r"1 to 80 characters on one line; this one is 0\."):
        export.plan(KEY, "linkedin-video", caption="")
    for mark in ("\n", "\r", "\u2028", "\u2029"):
        with pytest.raises(ValueError, match=r"1 to 80 characters on one line; this one has "
                                             r"a line break\."):
            export.plan(KEY, "linkedin-video", caption=f"one{mark}two")
    # Code points, not bytes; and drawn as given, neither trimmed nor escaped.
    assert export.plan(KEY, "linkedin-video", caption="é" * 80).caption == "é" * 80
    spaced = "  <b>Bold</b> &amp; more  "
    job = export.plan(KEY, "linkedin-video", caption=spaced)
    assert job.caption == spaced and _query(job.url)["caption"] == spaced


def test_bottom_right_is_refused_where_the_platform_covers_it():
    with pytest.raises(ValueError, match=r"^Choose another corner for the clock: on "
                                         r"instagram-reel, Instagram's button rail covers "
                                         r"the bottom right\.$"):
        export.plan(KEY, "instagram-reel", clock_corner="bottom-right")
    with pytest.raises(ValueError, match=r"on instagram-story, Instagram's bottom zone covers "
                                         r"the bottom right"):
        export.plan(KEY, "instagram-story", clock=True, clock_corner="bottom-right")
    # Judged only where the clock is drawn: a still of the story has none.
    assert export.plan(KEY, "instagram-story",
                       clock_corner="bottom-right").clock_corner == "bottom-right"


def test_top_left_is_refused_while_the_name_block_is_drawn():
    """This brief's rule, not the issue's: the name block sits top left, so a
    clock there would be drawn over the title or the caption."""
    with pytest.raises(ValueError, match="the title sits top left"):
        export.plan(KEY, "linkedin-video", clock_corner="top-left")
    with pytest.raises(ValueError, match="the caption sits top left"):
        export.plan(KEY, "linkedin-video", title=False, caption="Hi", clock_corner="top-left")
    job = export.plan(KEY, "linkedin-video", title=False, clock_corner="top-left")
    assert job.clock_corner == "top-left" and _query(job.url)["corner"] == "top-left"


def test_bottom_left_on_the_reel_comes_with_a_note():
    job = export.plan(KEY, "instagram-reel", clock_corner="bottom-left")
    assert job.clock_corner == "bottom-left" and _query(job.url)["corner"] == "bottom-left"
    assert len(job.notes) == 1
    assert "bottom zone" in job.notes[0] and "35%" in job.notes[0]
    assert export.plan(KEY, "linkedin-video", clock_corner="bottom-left").notes == ()


def test_a_corner_is_one_of_four():
    with pytest.raises(ValueError, match="top-left, top-right, bottom-left, bottom-right"):
        export.plan(KEY, "linkedin-video", clock_corner="middle")


def test_the_corner_defaults_to_today_except_where_the_platform_covers_it():
    reel = export.plan(KEY, "instagram-reel")
    assert reel.clock_corner == "top-right" and _query(reel.url)["corner"] == "top-right"
    linkedin = export.plan(KEY, "linkedin-video")
    assert linkedin.clock_corner == "bottom-right" and "corner" not in _query(linkedin.url)
    # The story's still resolves a corner it does not draw.
    story = export.plan(KEY, "instagram-story")
    assert story.clock_corner == "top-right" and "corner" not in _query(story.url)


@pytest.mark.parametrize("name,options", [
    ("instagram-reel", {}),
    ("instagram-reel", {"caption": "<b>Bold</b> &amp; more", "clock_corner": "bottom-left"}),
    ("instagram-story", {"clock": True, "caption": "Hi"}),
    ("linkedin-video", {}),
    ("linkedin-video", {"caption": "x" * 80, "clock_corner": "top-right"}),
    ("instagram-post", {"clock": True, "clock_corner": "bottom-left"}),
])
def test_the_job_carries_what_the_address_carries(name, options):
    job = export.plan(KEY, name, page=PAGE, date=DATE, **options)
    query = parse_qs(urlsplit(job.url).query)
    assert query["clock"] == ["1"]
    assert job.caption == query.get("caption", [None])[0]
    assert job.clock_corner == query.get("corner", ["bottom-right"])[0]
    assert job.to_dict()["caption"] == job.caption
    assert job.to_dict()["clock_corner"] == job.clock_corner


def test_a_preset_with_a_row_carries_its_fractions_on_every_address():
    """The export's address included. Inert without ``safe``: the page
    draws the zones only for the preview, so the plan never carries it."""
    reel = _query(export.plan(KEY, "instagram-reel").url)
    assert {k: reel[k] for k in ZONE_KEYS} == {
        "ztop": "0.14", "zbottom": "0.35", "zside": "0.06", "zrail": "0.21", "zrailtop": "0.6"}
    story = _query(export.plan(KEY, "instagram-story").url)
    assert {k: v for k, v in story.items() if k in ZONE_KEYS} == {"ztop": "0.14",
                                                                  "zbottom": "0.2"}
    post = _query(export.plan(KEY, "instagram-post").url)
    assert not set(ZONE_KEYS) & set(post)
    assert "safe" not in reel and "safe" not in story


# ------------------------------------------------------------- the page, as text


def _safe_area() -> str:
    js = PRESENT_JS.read_text(encoding="utf-8")
    return js[js.index("------- safe area\n"):]


def test_the_safe_area_holds_no_zone_number():
    """Every number is the address's: the block's only literals are 0 and
    100, and none of the old boxes' percentages is anywhere in the file."""
    code = re.sub(r"//[^\n]*", "", _safe_area())
    assert set(re.findall(r"(?<![\w.])\d+(?:\.\d+)?", code)) <= {"0", "100"}
    js = PRESENT_JS.read_text(encoding="utf-8")
    for old in ("12%", "22%", "18%", "40%", "38%"):
        assert old not in js, old


def test_the_caption_is_set_as_text():
    js = PRESENT_JS.read_text(encoding="utf-8")
    assert "captionEl.textContent = caption;" in js
    for parser in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert parser not in js, parser


# --------------------------------------------------------------- the page, drawn

# One browser for every address: each is opened in a fresh context at the
# frame's size, settled with the clock stopped as the recorder does, and
# measured. Rectangles are CSS pixels from getBoundingClientRect.
MEASURE = r"""
const { chromium } = require(process.argv[1]);
const job = JSON.parse(process.argv[2]);
(async () => {
  const browser = await chromium.launch({ channel: "chromium" });
  const out = [];
  try {
    for (const url of job.urls) {
      const ctx = await browser.newContext({ viewport: { width: job.width, height: job.height },
                                             reducedMotion: "no-preference" });
      const page = await ctx.newPage();
      const problems = [];
      page.on("pageerror", e => problems.push(e.message));
      await page.goto(url, { waitUntil: "load" });
      await page.waitForFunction(() => window.__present && window.__present.state);
      await page.evaluate(() => { window.__present.setCapture(true); window.__present.settle(); });
      await page.evaluate(() => document.fonts && document.fonts.ready);
      const seen = await page.evaluate(() => {
        const rect = el => {
          const r = el.getBoundingClientRect();
          return { top: r.top, bottom: r.bottom, left: r.left, right: r.right };
        };
        const box = document.getElementById("present-overlay");
        const name = box.querySelector(".name");
        const time = box.querySelector(".time");
        const caption = box.querySelector(".caption");
        const style = getComputedStyle(caption);
        return {
          name: name.hidden ? null : rect(name),
          time: time.hidden ? null : rect(time),
          caption: caption.hidden ? null : {
            text: caption.textContent, rect: rect(caption), clientHeight: caption.clientHeight,
            lineHeight: parseFloat(style.lineHeight), color: style.color },
          bold: [...box.querySelectorAll("b")].map(b => b.textContent),
          safe: [...document.querySelectorAll("#present-safe div")].map(rect),
          stations: [...document.querySelectorAll("#linear-stations circle")]
            .map(c => c.getBoundingClientRect()).filter(r => r.height > 0).map(r => r.top),
        };
      });
      seen.problems = problems;
      out.push(seen);
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(String(e.message).split("\n")[0]); process.exit(1); });
"""


def _page(into: Path) -> str:
    """A page drawn from a hand-made network, so no stored layout is needed:
    a line of five stations running north and a branch off its middle. It is
    taller than the reel's frame, so it fills the frame's height and reaches
    as near the top as the margin lets it."""
    points = {"n0": (0, 0), "n1": (0, 10), "n2": (0, 20), "n3": (0, 30), "n4": (0, 40),
              "n5": (20, 20)}
    edges = [("n0", "n1", "A"), ("n1", "n2", "A"), ("n2", "n3", "A"), ("n3", "n4", "A"),
             ("n2", "n5", "B")]
    colors = {"A": "0072bc", "B": "e8b04a"}
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": list(xy)},
              "properties": {"id": n, "station_id": "S" + n[1:],
                             "station_label": f"Stop {n[1:]}"}}
             for n, xy in points.items()]
    for a, b, line in edges:
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [list(points[a]), list(points[b])]},
                      "properties": {"from": a, "to": b,
                                     "lines": [{"id": line, "label": line,
                                                "color": colors[line]}]}})
    graph = LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})
    drawn = render(graph, title="Testville")

    def trip(trip_id: str, line: str, nodes: list[str], start: int) -> Trip:
        return Trip(trip_id, line, "end", [Call("S" + n[1:], n, start + i * 120,
                                                start + i * 120 + 20)
                                           for i, n in enumerate(nodes)])

    seven = 7 * 3600
    trips = [trip("a1", "A", ["n0", "n1", "n2", "n3", "n4"], seven - 300),
             trip("a2", "A", ["n4", "n3", "n2", "n1", "n0"], seven - 100),
             trip("b1", "B", ["n2", "n5"], seven - 60)]
    animation = animate.build(drawn, graph, trips, dt.date.fromisoformat(DATE))
    _, html = animate.write(animation, drawn.svg, into, stem="testville", name="Testville")
    return html.as_uri()


def _two_lines(caption: dict) -> bool:
    """Two lines exactly: more than one line-height, and at most two."""
    return caption["lineHeight"] < caption["clientHeight"] <= 2 * caption["lineHeight"]


def _without_zones(url: str) -> str:
    base, query = url.split("?", 1)
    return base + "?" + urlencode([(k, v) for k, v in parse_qsl(query) if k not in ZONE_KEYS])


@needs_browser
def test_the_page_draws_the_caption_as_text_the_corner_and_the_zones(tmp_path):
    page = _page(tmp_path)
    reel = export.PRESETS["instagram-reel"]
    row = export.SAFE_ZONES["instagram-reels"]
    width, height = reel.width, reel.height
    on_reel = dict(page=page, date=DATE, clock_corner="top-right", zones=row)
    markup = "<b>Bold</b> &amp; more"
    urls = [export.url_for(KEY, reel, caption=markup, **on_reel)]
    two_lines = [(text, theme) for text in (SENTENCE, SENTENCE.upper())
                 for theme in ("dark", "light")]
    urls += [export.url_for(KEY, reel, caption=text, theme=theme, **on_reel)
             for text, theme in two_lines]
    urls.append(export.url_for(KEY, reel, safe=True, **on_reel))
    planned = export.plan(KEY, "instagram-reel", page=page, date=DATE, caption=SENTENCE).url
    urls += [planned, _without_zones(planned)]
    assert len(SENTENCE) == 80 and _query(planned)["corner"] == "top-right"

    done = subprocess.run(["node", "-e", MEASURE, str(PLAYWRIGHT),
                           json.dumps({"width": width, "height": height, "urls": urls})],
                          capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert all(not s["problems"] for s in seen), [s["problems"] for s in seen]
    marked, *rest = seen
    wrapped, rest = rest[:4], rest[4:]
    safe, moved, unmoved = rest

    # The caption is the characters it was given, and adds no element: the
    # overlay's one <b> is the city's.
    assert marked["caption"]["text"] == markup
    assert marked["bold"] == ["Los Angeles"]

    # Eighty characters, a sentence or capitals, wrap to two lines at most
    # beside a top-right clock, in both themes, in the network's colour.
    muted = {"dark": "rgb(195, 184, 170)", "light": "rgb(101, 87, 72)"}
    for (text, theme), s in zip(two_lines, wrapped):
        caption = s["caption"]
        assert caption["text"] == text
        assert caption["clientHeight"] <= 2 * caption["lineHeight"], (theme, text, caption)
        assert caption["color"] == muted[theme]

    # Under safe=1, a box per fraction on the address and nothing else.
    expected = [
        (0, 0, width, row.top * height),                                   # top band
        (0, height - row.bottom * height, width, height),                  # bottom band
        (0, 0, row.side * width, height),                                  # left side
        (width - row.side * width, 0, width, height),                      # right side
        (width - row.rail_width * width, row.rail_top * height, width, height),  # the rail
    ]
    assert len(safe["safe"]) == len(expected)
    for got, (left, top, right, bottom) in zip(safe["safe"], expected):
        for edge, want in (("left", left), ("top", top), ("right", right), ("bottom", bottom)):
            assert abs(got[edge] - want) <= 1, (got, edge, want)

    # The reel's own address: the name block and the clock sit below the top
    # zone on today's 4.2vmin, off both side zones, and the network keeps
    # below the zone. Printed for the record (pytest -s).
    zone = row.top * height
    inset = row.side * width
    name, clock, caption = moved["name"], moved["time"], moved["caption"]
    print(f"\ntop zone {zone:.1f}px; name block {name['top']:.1f}-{name['bottom']:.1f}, "
          f"caption ends {caption['rect']['bottom']:.1f}, clock {clock['top']:.1f}-"
          f"{clock['bottom']:.1f}, highest station {min(moved['stations']):.1f}; without "
          f"the zones the name block is {unmoved['name']['top']:.1f}-"
          f"{unmoved['name']['bottom']:.1f} and the clock {unmoved['time']['top']:.1f}-"
          f"{unmoved['time']['bottom']:.1f}")
    assert _two_lines(caption), caption
    below = zone + 0.042 * min(width, height)                    # 314.16 at 1080x1920
    assert abs(name["top"] - below) <= 1 and abs(clock["top"] - below) <= 1
    for box in (name, clock):
        assert box["left"] >= inset and width - box["right"] >= inset, box
    assert moved["stations"] and min(moved["stations"]) >= zone
    assert abs(unmoved["name"]["top"] - 0.042 * min(width, height)) <= 1    # 45.36
