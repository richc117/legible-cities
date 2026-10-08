"""The site's sharing tags and the card they point at.

A link preview fails silently: the tags are either absent or relative, the page
looks perfect in a browser, and the only symptom is a bare link in somebody
else's chat window. So the things asserted here are the ones that cannot be
seen by opening the site -- that an origin exists, that both consumers of it
emit whole URLs, and that the card is the size it claims to be.
"""

import datetime as dt
import json
import re
import shutil
import struct
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

from schematic import animate, config, feeds, pipeline, site
from schematic.linegraph import LineGraph
from schematic.render import Style, render
from schematic.schedule import Call, Trip

SITE_JSON = site.SRC_DIR / "_data" / "site.json"
BASE_NJK = site.SRC_DIR / "_includes" / "layouts" / "base.njk"
PAGE = Path(site.__file__).parent / "page" / "page.html"
CARD = site.ASSETS_DIR / site.CARD_NAME


def test_the_site_knows_its_own_origin():
    """The one fact an unfurler needs and a browser never does.

    Slack, Teams and Messenger fetch og:image from their own servers, with no
    page to resolve a relative path against, so a root-relative card silently
    unfurls as no picture at all.
    """
    config = json.loads(SITE_JSON.read_text())
    assert config["origin"].startswith("https://")
    assert not config["origin"].endswith("/")        # composes with pathPrefix


def test_the_share_urls_are_absolute():
    for url in (site.card_url(), site.page_url("la-metro-rail")):
        assert url.startswith("https://")
    # The prefix is not written down twice: both go through path_prefix().
    assert site.path_prefix() in site.card_url()


def test_the_layout_carries_the_sharing_tags():
    head = BASE_NJK.read_text()
    for tag in ("og:title", "og:description", "og:url", "og:image",
                "og:image:width", "twitter:card", 'rel="canonical"'):
        assert tag in head, tag
    # Through the filter, never as a bare path -- that is the whole failure.
    assert "site.card.image | absolute" in head


def test_the_animation_page_keeps_its_placeholder():
    """The map pages are generated, so Eleventy's head never reaches them."""
    assert "__SOCIAL__" in PAGE.read_text()


EMBED_NJK = site.SRC_DIR / "_includes" / "map-embed.njk"

# The four views, in the order the argument runs in. Written down here because
# two switchers have to agree: the animation page's own toolbar and the copy the
# essay draws outside the iframe. `map` is labelled "Schematic" -- the word
# changed, the key did not, because it is in every atlas link and storyboard.
SWITCHER = [("geographic", "Geographic"), ("map", "Schematic"),
            ("linear", "Linear"), ("time", "Time")]


def test_both_switchers_offer_the_same_four_views_in_the_same_order():
    """A reader meets this control twice; it has to be the same control."""
    page = PAGE.read_text()
    ids = re.findall(r'<button id="view-(\w+)"', page)
    assert ids == ["geo", "map", "linear", "string"]

    njk = EMBED_NJK.read_text()
    pairs = re.findall(r'\["(\w+)", "(\w+)", "', njk)
    assert pairs == SWITCHER


def test_the_switcher_is_icons_with_an_accessible_name():
    """Icon-only on every device, so the name cannot ride on visible text."""
    for text, sel in ((PAGE.read_text(), 'id="view-'),
                      (EMBED_NJK.read_text(), 'data-view=')):
        # No visible label survives -- that variant is gone, not hidden.
        assert 'class="label"' not in text
        for button in re.findall(r"<button [^>]*>", text):
            if sel not in button:
                continue
            assert "aria-label=" in button, button
            assert "title=" in button, button
            assert "aria-pressed=" in button, button


def test_generated_tags_are_escaped_and_absolute():
    tags = site.social_tags("la-metro-rail")
    assert 'property="og:image"' in tags
    assert tags.count("https://") >= 4
    assert "&#x27;" in tags        # the apostrophe in "a real day's timetable"


def test_a_page_without_a_home_claims_none(monkeypatch):
    """A standalone page should not assert a URL it does not have."""
    monkeypatch.setattr(site, "origin", lambda: "")
    assert site.social_tags("la-metro-rail") == ""
    assert site.card_url() == ""


def test_the_card_is_the_size_the_tags_promise():
    """1200x630, or the tags lie about the image and clients letterbox it."""
    assert CARD.exists(), "the share card is committed; regenerate with site.og_card()"
    header = CARD.read_bytes()[:33]
    assert header[1:4] == b"PNG"
    assert struct.unpack(">II", header[16:24]) == site.CARD_SIZE
    config = json.loads(SITE_JSON.read_text())["card"]
    assert (config["width"], config["height"]) == site.CARD_SIZE
    # Every one of these platforms has a size ceiling; none is near a megabyte.
    assert CARD.stat().st_size < 1_000_000


@pytest.mark.skipif(not (site.MAPS_DIR / "la-metro-rail.html").exists(),
                    reason="site/src/maps is generated and gitignored")
def test_a_generated_map_page_carries_an_absolute_card():
    page = (site.MAPS_DIR / "la-metro-rail.html").read_text(errors="ignore")
    head = page[:4000]
    match = re.search(r'<meta property="og:image" content="([^"]+)"', head)
    assert match, "the map pages were generated before the tags existed"
    assert match.group(1).startswith("https://")
    assert match.group(1).endswith(site.CARD_NAME)


def test_the_manifest_survives_the_subpath():
    """A passthrough asset never sees Eleventy's `url` filter.

    So the manifest cannot carry root-relative paths the way the head can --
    under /legible-cities/ they point at the wrong origin and 404. Relative
    members resolve against the manifest's own URL, which is correct at any
    prefix and at the root.
    """
    manifest = json.loads((site.ASSETS_DIR / "favicon" / "site.webmanifest").read_text())
    assert not manifest["start_url"].startswith("/")
    assert not manifest["scope"].startswith("/")
    for icon in manifest["icons"]:
        assert not icon["src"].startswith("/"), icon["src"]
        assert (site.ASSETS_DIR / "favicon" / icon["src"]).exists()


def test_the_card_is_drawn_from_a_real_network():
    assert site.FEATURED in feeds.FEEDS


# ------------------------------------------------------------------- the atlas


def test_the_issue_score_is_a_proportion_not_a_count():
    """Ranking by raw totals would just re-sort the atlas by size again."""
    assert set(site.ISSUE_WEIGHTS) == {"unplaced", "unrouted", "skipped",
                                       "borrowed", "unlabelled"}
    # Structural failures outrank cosmetic ones: a stop the map cannot place
    # costs more than a name it cannot draw.
    assert site.ISSUE_WEIGHTS["unplaced"] > site.ISSUE_WEIGHTS["unlabelled"]
    assert site.ISSUE_WEIGHTS["unrouted"] > site.ISSUE_WEIGHTS["borrowed"]


def test_the_atlas_leads_with_the_essay_then_runs_cleanest_first():
    """The atlas is the essay's appendix before it is a league table.

    Ordering by size opened it on New York, which is both the biggest network
    and the one carrying the most caveats -- so the page led with its worst case
    and buried the maps that came out perfectly.
    """
    data = site.DATA_DIR / "networks.json"
    if not data.exists():
        pytest.skip("site not built")
    entries = json.loads(data.read_text())["networks"]

    assert [e["key"] for e in entries[:2]] == list(site.LEADS)
    rest = [e["issues"] for e in entries[2:]]
    assert rest == sorted(rest), "the tail is not cleanest-first"
    # A clean network really is clean, and the score means what it says.
    assert rest[0] == 0.0
    for e in entries:
        assert e["issues"] >= 0
        if not e["caveats"]:
            assert e["issues"] == 0.0, e["key"]


def test_every_top_level_page_marks_itself_current_in_the_nav():
    """The brand is the home link, so it needs the mark the other two carry.

    Without it the landing page was the one page whose header said nothing
    about where you were.
    """
    head = BASE_NJK.read_text()
    assert 'page.url == "/"' in head, "the brand carries no current-page test"
    # One rule, both halves of the header -- the brand sits outside .site-nav.
    css = (site.ASSETS_DIR / "style.css").read_text()
    assert '.brand[aria-current="page"]' in css
    assert '.site-nav a[aria-current="page"]' in css


def test_only_the_presentation_link_asks_for_the_switcher():
    """Three things open present mode; exactly one of them wants controls.

    The Presentation link is a page a reader steers. The essay's iframes draw
    their own switcher outside the frame, so one inside would double it. And an
    export captures whatever is on screen -- guarded on the Python side by
    test_no_export_url_ever_asks_for_the_controls.
    """
    njk = EMBED_NJK.read_text()
    link, iframe = njk.split("{% macro embed(")
    assert "controls=1" in link, "the Presentation link does not ask for it"
    assert "controls=1" not in iframe, "the essay's iframe would double the switcher"
    # The link's tooltip promised no interface at all; it has one now.
    assert "with no interface around it" not in njk


def test_the_page_only_shows_the_switcher_when_asked():
    """Present mode still hides the header by default -- controls=1 is opt-in."""
    page = PAGE.read_text()
    assert ":root[data-present] header { display: none; }" in page
    assert ':root[data-present][data-controls] header {' in page
    js = (Path(site.__file__).parent / "page" / "present.js").read_text()
    assert 'on("controls", false)' in js, "the flag must default off"



# ----------------------------------------------------- the header, accessibly
#
# Engine issue 34: the controls a reader meets on the running map. The first
# group reads the template and needs no browser; the second draws a page from a
# hand-made network and drives it from node with the full Chromium, as
# bin/_record.js does, so the keyboard is the keyboard and the focus ring is a
# drawn one. The page's own seam (window.__present) is only ever read here.

CSS_SITE = site.ASSETS_DIR / "style.css"
PLAYWRIGHT = config.REPO_ROOT / "site" / "node_modules" / "playwright"

# Written here and not imported from tests/test_determinism.py: this file draws
# its own pages, so it needs no built map, only the browser.
needs_browser = pytest.mark.skipif(
    not (PLAYWRIGHT.exists() and shutil.which("node")),
    reason="needs site/node_modules/playwright")


def _header() -> str:
    page = PAGE.read_text(encoding="utf-8")
    return page[page.index("<header>"):page.index("</header>")]


def _tokens(page: str) -> dict[str, dict[str, str]]:
    """The colours each theme's :root rule sets, by the theme's own name."""
    warm = re.search(r"\n  :root \{(.*?)\n  \}", page, re.S).group(1)
    sepia = re.search(r':root\[data-theme="sepia"\] \{(.*?)\n  \}', page, re.S).group(1)
    return {theme: dict(re.findall(r"--([\w-]+): (#[0-9a-fA-F]{6})", body))
            for theme, body in (("warm-dark", warm), ("sepia", sepia))}


def _luminance(colour: str) -> float:
    def part(i: int) -> float:
        c = int(colour[1 + i:3 + i], 16) / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * part(0) + 0.7152 * part(2) + 0.0722 * part(4)


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _rgb(colour: str) -> str:
    return "rgb({}, {}, {})".format(*(int(colour[i:i + 2], 16) for i in (1, 3, 5)))


class _Regions(HTMLParser):
    """Every element of a piece of markup that is a live region, and the ids
    of everything inside one (the region itself included)."""

    VOID = {"input", "br", "img", "meta", "link", "hr"}

    def __init__(self):
        super().__init__()
        self.stack: list[dict] = []
        self.regions: list[dict] = []
        self.inside: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        entry = {"tag": tag, "id": attrs.get("id"), "live": attrs.get("aria-live"),
                 "atomic": attrs.get("aria-atomic")}
        if entry["live"] is not None:
            self.regions.append(entry)
        live_here = entry["live"] is not None or any(e["live"] is not None for e in self.stack)
        if entry["id"] and live_here:
            self.inside.append(entry["id"])
        if tag not in self.VOID:
            self.stack.append(entry)

    def handle_endtag(self, tag):
        while self.stack:
            if self.stack.pop()["tag"] == tag:
                break


def test_play_is_a_plain_button_that_says_what_pressing_it_does():
    """A pressed state and a name that flips with it say the same thing twice,
    and a screen reader hears "Pause, toggle button, pressed"."""
    page = PAGE.read_text(encoding="utf-8")
    play = re.search(r'<button id="play"[^>]*>', page).group(0)
    assert "aria-pressed" not in play, play
    assert 'playBtn.setAttribute("aria-pressed"' not in page
    assert '#play[aria-pressed' not in page, "the rule for a state the button no longer has"
    assert 'playBtn.textContent = playing ? "Pause" : "Play"' in page


def test_one_polite_live_region_and_the_clock_is_not_in_it():
    """The clock changes every frame; a region that held it would speak all day.
    The region says "Playing" or "Paused" when the button is pressed, and the
    script writes it nowhere else."""
    page = PAGE.read_text(encoding="utf-8")
    found = _Regions()
    found.feed(_header())
    assert len(found.regions) == 1, found.regions
    region = found.regions[0]
    assert region["live"] == "polite" and region["atomic"] == "true", region
    assert region["id"] != "clock", "the clock is the live region"
    assert "clock" not in found.inside and "count" not in found.inside, found.inside
    # Nothing else speaks unprompted: no other region by attribute, by role or
    # from the script, and one write, inside the button's handler.
    assert page.count("aria-live=") == 1
    assert "aria-live" not in page[page.index("<script>\n(() =>"):]
    for role in ("status", "alert", "log", "timer", "marquee"):
        assert f'role="{role}"' not in page, role
    assert page.count("announce.textContent") == 1
    handler = page[page.index("playBtn.onclick"):]
    assert "announce.textContent" in handler[:handler.index("};")]


def test_the_scrub_reads_as_a_time_and_not_as_a_count_of_seconds():
    page = PAGE.read_text(encoding="utf-8")
    assert re.findall(r'scrub\.setAttribute\("aria-valuetext", (\w+)\)', page) == ["when"]
    assert "const when = fmt(now);" in page
    assert "clock.textContent = when;" in page, "the scrub and the clock read different text"


def test_the_focus_rule_names_every_kind_of_focusable_thing_in_the_header():
    """The chips are spans with a role, which `a, button, input` do not name;
    left out, they fall back on the browser's own ring, a dark blue that does
    not show on the warm dark ground."""
    page = PAGE.read_text(encoding="utf-8")
    rule = re.search(
        r"\n  ([^{}]+?)\s*\{\s*outline: 2px solid var\(--focus\); outline-offset: 2px;", page)
    assert rule, "the page's focus rule moved"
    selectors = {s.strip() for s in rule.group(1).split(",")}
    assert {"a:focus-visible", "button:focus-visible", "input:focus-visible",
            ".chip:focus-visible"} <= selectors, selectors


def test_the_focus_ring_clears_three_to_one_on_every_ground_it_sits_on():
    """--focus on the header (--bg), on the switcher's own ground (--bg-soft),
    and, where a ring sits on a pressed button, on --bg again: the pressed fill
    is --text, which the ring does not clear in either theme, so a band of --bg
    separates them. Both switchers draw it, and in the same way."""
    page = PAGE.read_text(encoding="utf-8")
    for theme, token in _tokens(page).items():
        for ground in ("bg", "bg-soft"):
            ratio = _contrast(token["focus"], token[ground])
            assert ratio >= 3, (theme, ground, round(ratio, 2))

    squash = lambda text: re.sub(r"\s+", " ", text)
    site_css = squash(CSS_SITE.read_text(encoding="utf-8"))
    ring = ".segmented button:focus-visible { outline-offset: -2px; }"
    pressed = ('.segmented button[aria-pressed="true"]:focus-visible '
               '{ box-shadow: inset 0 0 0 4px var(--bg); }')
    for where, text in (("page", squash(page)), ("site", site_css)):
        for rule in (
            # the site's rule draws the ring itself; the page's shares the focus rule's
            ring.replace("{ ", "{ outline: 2px solid var(--focus); ") if where == "site" else ring,
            ".segmented button:first-child:focus-visible { border-radius: 999px 0 0 999px; }",
            ".segmented button:last-child:focus-visible { border-radius: 0 999px 999px 0; }",
            pressed,
        ):
            assert rule in text, (where, rule)


def test_the_script_measures_chip_dots_against_the_pages_own_grounds():
    """The two grounds are written into the script, which cannot read a theme
    that is not showing. They are the --bg of each theme's rule, or a dot is
    ringed against a colour the header is not."""
    page = PAGE.read_text(encoding="utf-8")
    literal = re.search(r"const GROUNDS = \{(.*?)\};", page, re.S).group(1)
    grounds = {k: v.lower() for k, v in re.findall(r'"([\w-]+)": "(#[0-9a-fA-F]{6})"', literal)}
    assert grounds == {theme: token["bg"].lower() for theme, token in _tokens(page).items()}


def test_a_weak_dot_is_ringed_by_a_rule_for_each_theme():
    css = re.sub(r"\s+", " ", PAGE.read_text(encoding="utf-8"))
    assert (':root:not([data-theme="sepia"]) .chip .dot[data-weak~="warm-dark"], '
            ':root[data-theme="sepia"] .chip .dot[data-weak~="sepia"] '
            '{ box-shadow: 0 0 0 1px var(--text); }') in css


def test_a_name_holds_what_the_control_shows_and_reads_no_arrow_aloud():
    page, header = PAGE.read_text(encoding="utf-8"), _header()
    assert 'class="back" href="__BACK__" aria-label="Back to the atlas"' in header
    # The arrows are decoration, in the markup and in the script that swaps them.
    assert re.search(r'<button id="more"[^>]*>More <span aria-hidden="true">&#9662;</span>', header)
    for word, arrow in (("Less", "&#9652;"), ("More", "&#9662;")):
        assert f"{word} <span aria-hidden=\"true\">{arrow}</span>" in page, word
    # The speed's name contains the rate it shows ("60×"), as written at every change.
    assert 'speedBtn.setAttribute("aria-label", "Playback speed " + speed + "\\u00d7");' in page
    assert 'aria-label="Playback speed 60&times;"' in header
    # A span has no role to take a name from; the unit is read as text.
    assert 'count.setAttribute("aria-label"' not in page


def test_under_reduced_motion_the_trains_start_paused_and_a_click_lands_at_once():
    page = PAGE.read_text(encoding="utf-8")
    assert "playing = !reduced" in page
    assert "const CLICK_DUR = reduced ? 0 : CLICK_MORPH;" in page
    # Its own length is read once, here: every click takes CLICK_DUR, never CLICK_MORPH.
    assert page.count("CLICK_MORPH") == 2, "a click's length reaches the page some other way"
    assert page.count("setMode(k, CLICK_DUR)") == 1 and page.count('setMode("map", CLICK_DUR)') == 1
    # present mode is the one place `reduced` is false whatever the person prefers
    assert ('const reduced = !present && '
            'matchMedia("(prefers-reduced-motion: reduce)").matches;') in page


# A line of five stations running north, a branch off its middle and a spur off
# that, three lines in three colours: the blue clears 3:1 on both grounds, the
# gold does not on the sepia one and the near-black does not on the warm dark one.
# The service day runs three hours, so a clock read twice in a test does not meet
# the wrap at its end.
WEAK = {"A": "0072bc", "B": "ffd700", "C": "2a2a2a"}


def _hand_made_page(into: Path) -> str:
    points = {"n0": (0, 0), "n1": (0, 10), "n2": (0, 20), "n3": (0, 30), "n4": (0, 40),
              "n5": (20, 20), "n6": (20, 30)}
    edges = [("n0", "n1", "A"), ("n1", "n2", "A"), ("n2", "n3", "A"), ("n3", "n4", "A"),
             ("n2", "n5", "B"), ("n5", "n6", "C")]

    def graph(at: dict[str, tuple[float, float]]) -> LineGraph:
        feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": list(xy)},
                  "properties": {"id": n, "station_id": "S" + n[1:],
                                 "station_label": f"Stop {n[1:]}"}}
                 for n, xy in at.items()]
        for a, b, line in edges:
            feats.append({"type": "Feature",
                          "geometry": {"type": "LineString",
                                       "coordinates": [list(at[a]), list(at[b])]},
                          "properties": {"from": a, "to": b,
                                         "lines": [{"id": line, "label": line,
                                                    "color": WEAK[line]}]}})
        return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})

    network = graph(points)
    drawn = render(network, title="Testville")
    # The same stations nudged, standing for the network before octi straightened
    # it, so the page has the geographic view and its button.
    nudged = {"n0": (3, 1), "n1": (-2, 12), "n2": (5, 19), "n3": (-4, 33), "n4": (2, 47),
              "n5": (26, 14), "n6": (18, 36)}
    geo = animate.geographic_tracks(graph(nudged), network, drawn)

    def trip(trip_id: str, line: str, nodes: list[str], start: int) -> Trip:
        return Trip(trip_id, line, "end", [Call("S" + n[1:], n, start + i * 120,
                                                start + i * 120 + 20)
                                           for i, n in enumerate(nodes)])

    seven = 7 * 3600
    trips = [trip("a1", "A", ["n0", "n1", "n2", "n3", "n4"], seven - 300),
             trip("a2", "A", ["n4", "n3", "n2", "n1", "n0"], seven - 100),
             trip("b1", "B", ["n2", "n5"], seven - 60),
             trip("c1", "C", ["n5", "n6"], seven - 30),
             trip("a3", "A", ["n0", "n1", "n2", "n3", "n4"], seven + 3 * 3600)]
    animation = animate.build(drawn, network, trips, dt.date(2026, 9, 10), geo=geo)
    assert animation.geo and len(animation.trips) == 5
    _, html = animate.write(animation, drawn.svg, into, stem="testville", name="Testville")
    return html.as_uri()


def _run(script: str, job: dict) -> dict:
    done = subprocess.run(["node", "-e", script, str(PLAYWRIGHT), json.dumps(job)],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# Every script opens a page in the full Chromium, waits for its first frame (the
# clock reads --:-- until one is drawn) and counts nothing as passed that threw.
BROWSER = r"""
const { chromium } = require(process.argv[1]);
const job = JSON.parse(process.argv[2]);
const ready = page => page.waitForFunction(
  () => window.__present && window.__present.state().clock !== "--:--");
const frames = (page, n) => page.evaluate(n => new Promise(done => {
  const step = k => k ? requestAnimationFrame(() => step(k - 1)) : done();
  step(n);
}), n);
const now = page => page.evaluate(() => window.__present.state().now);
// What has focus, by the name this suite gives it: an id, else its class and text.
const active = page => page.evaluate(() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  const text = (el.textContent || "").trim().slice(0, 12);
  return el.id || el.tagName.toLowerCase() + "." + el.className + ":" + text;
});
async function main(run) {
  const browser = await chromium.launch({ channel: "chromium" });
  try {
    console.log(JSON.stringify(await run(browser)));
  } finally {
    await browser.close();
  }
}
const fail = e => { console.error(String(e.message).split("\n")[0]); process.exit(1); };
"""

PAUSE = BROWSER + r"""
main(async browser => {
  const out = { problems: [], order: [], results: [] };
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 },
                                         reducedMotion: "no-preference" });
  const page = await ctx.newPage();
  page.on("pageerror", e => out.problems.push(e.message));
  const open = async () => { await page.goto(job.url, { waitUntil: "load" }); await ready(page); };

  // The Tab order as the browser has it: from the top of the document until it
  // comes back to where it began. The edge of the document is a stop of its own
  // (focus goes to the browser's chrome), which reads as nothing here.
  await open();
  for (let i = 0; i < 80; i++) {
    await page.keyboard.press("Tab");
    const here = await active(page);
    if (here === null) continue;
    if (out.order.length && here === out.order[0]) break;
    out.order.push(here);
  }
  const n = out.order.length;

  // From every place focus can be, and from none: reach Play by Tab, within as
  // many presses as there are things that take focus (and the edge), by Shift+Tab
  // if need be, press Enter, and the clock stays where it stopped.
  for (let from = -1; from < n; from++) {
    await open();
    for (let k = 0; k <= from; k++) await page.keyboard.press("Tab");
    const r = { from: from < 0 ? "(nothing)" : out.order[from], presses: 0, via: "",
                reached: false };
    const first = await now(page);
    r.reached = (await active(page)) === "play";
    for (const key of ["Tab", "Shift+Tab"]) {
      for (let k = 0; !r.reached && k < n + 1; k++) {
        await page.keyboard.press(key);
        r.presses++; r.via = key;
        r.reached = (await active(page)) === "play";
      }
    }
    if (r.reached) {
      await page.waitForTimeout(100);
      r.moving = (await now(page)) > first;
      await page.keyboard.press("Enter");
      await frames(page, 2);
      r.stopped = await now(page);
      await page.waitForTimeout(300);
      r.later = await now(page);
      r.live = await page.evaluate(() => document.querySelector("[aria-live]").textContent);
      r.label = await page.evaluate(() => document.getElementById("play").textContent);
      await page.keyboard.press("Tab");
      await page.keyboard.press("Tab");
      await page.waitForTimeout(150);
      r.movedOn = await now(page);
      r.liveMovedOn = await page.evaluate(() => document.querySelector("[aria-live]").textContent);
    }
    out.results.push(r);
  }
  return out;
}).catch(fail);
"""


@needs_browser
def test_pause_works_by_keyboard_from_any_focus_position(tmp_path):
    seen = _run(PAUSE, {"url": _hand_made_page(tmp_path)})
    assert not seen["problems"], seen["problems"]
    order = seen["order"]
    assert "play" in order, f"Tab never reaches Play: {order}"
    # The header's controls at least; whatever else takes focus is in the walk too.
    assert len(order) >= 14, order
    assert len(seen["results"]) == len(order) + 1
    for r in seen["results"]:
        assert r["reached"], f"Play is out of reach from {r['from']}: {r}"
        assert r["moving"], f"the clock was not running before the press: {r}"
        assert r["later"] == r["stopped"], f"the clock kept moving after Pause: {r}"
        assert r["live"] == "Paused" and r["label"] == "Play", r
        # Paused stays paused when focus moves on, and nothing more is said.
        assert r["movedOn"] == r["stopped"] and r["liveMovedOn"] == "Paused", r


REDUCED = BROWSER + r"""
main(async browser => {
  const out = [];
  for (const run of job.runs) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 },
                                           reducedMotion: "no-preference" });
    const page = await ctx.newPage();
    const seen = { name: run.name, problems: [] };
    page.on("pageerror", e => seen.problems.push(e.message));
    // Before the page loads, so its first read of the preference is the emulated one.
    if (run.reduce) await page.emulateMedia({ reducedMotion: "reduce" });
    await page.goto(job.url + run.query, { waitUntil: "load" });
    await ready(page);
    seen.live = await page.evaluate(() => document.querySelector("[aria-live]").textContent);
    seen.play = await page.evaluate(() => document.getElementById("play").textContent);
    seen.now0 = await now(page);
    await page.waitForTimeout(500);
    seen.now1 = await now(page);
    if (run.click) {
      await page.click(run.click);
      await frames(page, 2);
      seen.after = await page.evaluate(() => { const s = window.__present.state();
        return { str: s.str, view: s.view, geo: s.geo, mode: s.mode }; });
    }
    if (run.press) {
      await page.click("#play");
      await page.waitForTimeout(300);
      seen.nowPlayed = await now(page);
      seen.livePlayed = await page.evaluate(
        () => document.querySelector("[aria-live]").textContent);
      seen.playPlayed = await page.evaluate(() => document.getElementById("play").textContent);
    }
    out.push(seen);
    await ctx.close();
  }
  return out;
}).catch(fail);
"""


@needs_browser
def test_reduced_motion_stops_the_trains_and_the_fade_and_leaves_present_mode_alone(tmp_path):
    url = _hand_made_page(tmp_path)
    runs = [
        {"name": "reduced", "reduce": True, "query": "", "click": "#view-string", "press": True},
        {"name": "reduced geo", "reduce": True, "query": "", "click": "#view-geo"},
        {"name": "plain", "reduce": False, "query": "", "click": "#view-string"},
        {"name": "plain geo", "reduce": False, "query": "", "click": "#view-geo"},
        {"name": "present, reduced", "reduce": True, "query": "?present=1"},
        {"name": "present", "reduce": False, "query": "?present=1"},
    ]
    seen = {r["name"]: r for r in _run(REDUCED, {"url": url, "runs": runs})}
    assert all(not r["problems"] for r in seen.values()), [r["problems"] for r in seen.values()]

    # The trains start paused and the button says what it would do. Nothing was
    # said at boot, and the clock does not move.
    reduced = seen["reduced"]
    assert reduced["play"] == "Play" and reduced["live"] == ""
    assert reduced["now1"] == reduced["now0"]
    # A click on Time lands on the next frame, with no fade into the chart; so does Geographic.
    assert reduced["after"]["str"] == 1 and reduced["after"]["mode"] == "string"
    assert seen["reduced geo"]["after"]["geo"] == 1
    # A reader can still press Play.
    assert reduced["nowPlayed"] > reduced["now1"]
    assert reduced["playPlayed"] == "Pause" and reduced["livePlayed"] == "Playing"

    # The control: without the preference the same clicks are the morph they were.
    plain = seen["plain"]
    assert plain["play"] == "Pause" and plain["now1"] > plain["now0"]
    assert 0 < plain["after"]["str"] < 1, plain["after"]
    assert 0 < seen["plain geo"]["after"]["geo"] < 1

    # Present mode ignores the preference, which is what keeps every export as it was.
    for name in ("present, reduced", "present"):
        assert seen[name]["now1"] > seen[name]["now0"], seen[name]


NAMES = BROWSER + r"""
main(async browser => {
  const out = {};
  for (const theme of ["warm-dark", "sepia"]) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 },
                                           reducedMotion: "no-preference" });
    const page = await ctx.newPage();
    const cdp = await ctx.newCDPSession(page);
    const seen = { problems: [], stops: [], dots: {} };
    // The browser's own accessibility node for an expression's element.
    const axNode = async expression => {
      const { result } = await cdp.send("Runtime.evaluate", { expression });
      const { nodes } = await cdp.send("Accessibility.getPartialAXTree",
                                       { objectId: result.objectId, fetchRelatives: false });
      return nodes[0];
    };
    page.on("pageerror", e => seen.problems.push(e.message));
    await page.goto(job.url + "?theme=" + theme, { waitUntil: "load" });
    await ready(page);
    const stops = new Set();
    for (let i = 0; i < 80; i++) {
      await page.keyboard.press("Tab");
      const here = await active(page);
      if (here === null) continue;
      if (stops.has(here)) break;
      stops.add(here);
      const info = await page.evaluate(() => {
        const el = document.activeElement, cs = getComputedStyle(el);
        const text = (el.textContent || "").trim();
        // A chip is named for its line, the one control with neither an id nor a class of its own.
        const named = el.className === "chip" ? ":" + text : "";
        return { id: el.id || el.tagName.toLowerCase() + "." + el.className + named,
                 text: text, clock: document.getElementById("clock").textContent,
                 outline: [cs.outlineStyle, cs.outlineWidth, cs.outlineColor, cs.outlineOffset],
                 shadow: cs.boxShadow, pressed: el.getAttribute("aria-pressed"),
                 valuetext: el.getAttribute("aria-valuetext") };
      });
      const node = await axNode("document.activeElement"), props = {};
      for (const p of node.properties || []) props[p.name] = p.value.value;
      info.role = node.role && node.role.value;
      info.name = node.name ? node.name.value : "";
      info.props = props;
      seen.stops.push(info);
    }
    // A key held on a chip is one press: two keydowns, the second its repeat, flip it once.
    await page.focus(".chip");
    seen.held = [await page.getAttribute(".chip", "aria-pressed")];
    await page.keyboard.down("Space");
    await page.keyboard.down("Space");
    await page.keyboard.up("Space");
    seen.held.push(await page.getAttribute(".chip", "aria-pressed"));
    // A name follows what a control now does: Play after Pause, the rate after a
    // press of the speed, More after Less. Each is a press and a read-back.
    const nameOf = async selector => {
      const node = await axNode(`document.querySelector("${selector}")`);
      return { name: node.name.value.trim(), props: node.properties.map(p => p.name) };
    };
    seen.after = {};
    for (const selector of ["#play", "#speed", "#more"]) {
      await page.click(selector);
      seen.after[selector] = await nameOf(selector);
      seen.after[selector].text = (await page.textContent(selector)).trim();
    }
    seen.said = await page.evaluate(() => document.querySelector("[aria-live]").textContent);
    // Every line's dot, in this theme and then in the other, changed in place.
    const dots = () => page.evaluate(() => [...document.querySelectorAll(".chip")].map(c => {
      const d = c.querySelector(".dot");
      return { line: c.textContent.trim(), weak: d.getAttribute("data-weak"),
               shadow: getComputedStyle(d).boxShadow };
    }));
    seen.dots[theme] = await dots();
    const other = theme === "sepia" ? "warm-dark" : "sepia";
    await page.evaluate(t => window.__present.setTheme(t), other);
    seen.dots[other] = await dots();
    out[theme] = seen;
    await ctx.close();
  }
  return out;
}).catch(fail);
"""


@needs_browser
def test_every_control_has_a_role_a_name_a_ring_and_a_state_in_both_themes(tmp_path):
    page_text = PAGE.read_text(encoding="utf-8")
    tokens = _tokens(page_text)
    seen = _run(NAMES, {"url": _hand_made_page(tmp_path)})

    chips = ["span.chip:A", "span.chip:B", "span.chip:C"]
    # What is pressed or not, and says so.
    toggles = ("labels-toggle", "view-geo", "view-map", "view-linear", "view-string")
    expected = [
        ("a.back", "link", "Back to the atlas"),
        ("view-geo", "button", "Geographic"), ("view-map", "button", "Schematic"),
        ("view-linear", "button", "Linear"), ("view-string", "button", "Time"),
        ("play", "button", "Pause"), ("scrub", "slider", "Time of day"),
        ("speed", "button", "Playback speed 60×"), ("more", "button", "Less"),
        ("lines-all", "button", "All"), ("lines-none", "button", "None"),
        *[(c, "button", c[-1]) for c in chips], ("labels-toggle", "button", "Labels"),
    ]
    for theme, token in tokens.items():
        run = seen[theme]
        assert not run["problems"], run["problems"]
        # The page's own controls, in reading order. The stage, a scroller the
        # browser makes a Tab stop, comes last and is the stage's, not the header's.
        header = [s for s in run["stops"] if s["id"] != "stage"]
        assert [(s["id"], s["role"], s["name"].strip()) for s in header] == expected

        for s in header:
            who = s["id"]
            # A 2px ring in the page's own --focus, drawn: not the browser's default.
            style, width, colour, _ = s["outline"]
            ring = ("solid", "2px", _rgb(token["focus"]))
            assert (style, width, colour) == ring, (theme, who, s["outline"])
            # What it shows is in its name (WCAG 2.5.3), the arrows aside.
            shown = re.sub(r"[▴▾←]", "", s["text"]).strip().lower()
            assert shown in s["name"].lower(), (theme, who, s["text"], s["name"])
            # A control that is pressed says so, and Play, which names what it does, does not.
            if s["id"] == "play":
                assert "pressed" not in s["props"], (theme, s["props"])
            elif s["id"] in toggles or s["text"] in ("A", "B", "C"):
                assert "pressed" in s["props"], (theme, who, s["props"])
            if s["id"] == "more":
                assert s["props"].get("expanded") is True
                assert s["props"].get("controls") == "more-panel"

        by_id = {s["id"]: s for s in header if s["id"]}
        # The scrub says the clock's own time (read in one task with the clock, so no
        # frame falls between them). The attribute is what is read back: the
        # browser's accessibility tree, over the debugging protocol, leaves a range's
        # value text out whatever the page writes, so what a screen reader is told is
        # for a person with one to check.
        assert by_id["scrub"]["valuetext"] == by_id["scrub"]["clock"], by_id["scrub"]
        # A pressed switcher button, focused, has the band of --bg between ring and fill.
        assert by_id["view-map"]["pressed"] == "true" and "inset" in by_id["view-map"]["shadow"]
        assert by_id["view-linear"]["pressed"] == "false"
        assert by_id["view-linear"]["shadow"] == "none"

        # After a press: Play now says what it will do, and the page said "Paused"
        # once; the rate is in the speed's name and its text; the panel's button
        # reads More.
        after = run["after"]
        assert after["#play"]["name"] == "Play" and "pressed" not in after["#play"]["props"]
        assert run["said"] == "Paused"
        assert run["held"] == ["true", "false"], "a held key flipped a chip more than once"
        assert after["#speed"]["text"] == "240\u00d7"
        assert after["#speed"]["name"] == "Playback speed 240\u00d7"
        assert after["#more"]["name"] == "More" and after["#more"]["text"].startswith("More")

    # A weak dot has a ring in the theme it is weak in and only there, for every
    # line and in both themes, the theme changed in place as well as at load. The
    # colours are the page's; whether each is weak is worked out here, apart from the page.
    weak = {line: {t for t, tk in tokens.items() if _contrast("#" + colour, tk["bg"]) < 3}
            for line, colour in WEAK.items()}
    assert weak == {"A": set(), "B": {"sepia"}, "C": {"warm-dark"}}, "the test's colours moved"
    for first in tokens:
        for showing, dots in seen[first]["dots"].items():
            for dot in dots:
                marked = set((dot["weak"] or "").split())
                assert marked == weak[dot["line"]], (showing, dot)
                ringed = dot["shadow"] != "none"
                assert ringed == (showing in weak[dot["line"]]), (first, showing, dot)
                if ringed:
                    assert _rgb(tokens[showing]["text"]) in dot["shadow"] and "1px" in dot["shadow"]


# ---------------------------------------------------------- the rows' order
#
# Issue 63. `map.build` takes a line order and the engine lays the lines out in
# it, but the page sorted its rows A-Z whatever the data said. The page is now
# told, by `arranged`, only when the map was built with an order, and then shows
# a third sort, pressed by default. These pages hold four lines whose three
# orders all differ: Python sorts the labels 1, 10, 2, B (the order an unordered
# map's layout is written in), the page's own A-Z reads 1, 2, 10, B, and by
# station count they run 10, 1, 2, B.

NUMBERED = {"1": 3, "2": 3, "10": 4, "B": 2}
ENGINES = ["1", "10", "2", "B"]
A_TO_Z = ["1", "2", "10", "B"]
BY_SIZE = ["10", "1", "2", "B"]


def _numbered_page(into: Path, line_order: list[str] | None = None):
    """The page of ``NUMBERED``'s lines, built with ``line_order``, and the
    animation it was written from."""
    features, trips = [], []
    seven = 7 * 3600
    for i, (label, size) in enumerate(NUMBERED.items()):
        for j in range(size):
            features.append({"type": "Feature",
                             "geometry": {"type": "Point", "coordinates": [30.0 * i, 10.0 * j]},
                             "properties": {"id": f"n{i}_{j}", "station_id": f"S{i}_{j}",
                                            "station_label": f"Stop {i}.{j}"}})
        for j in range(size - 1):
            features.append({"type": "Feature",
                             "geometry": {"type": "LineString",
                                          "coordinates": [[30.0 * i, 10.0 * j],
                                                          [30.0 * i, 10.0 * (j + 1)]]},
                             "properties": {"from": f"n{i}_{j}", "to": f"n{i}_{j + 1}",
                                            "lines": [{"id": label, "label": label,
                                                       "color": "0072bc"}]}})
        trips.append(Trip(f"t{i}", label, "end",
                          [Call(f"S{i}_{j}", f"n{i}_{j}", seven + j * 120, seven + j * 120 + 20)
                           for j in range(size)]))
    network = LineGraph.from_geojson({"type": "FeatureCollection", "features": features})
    drawn = render(network, title="Numbered")
    animation = animate.build(drawn, network, trips, dt.date(2026, 9, 10), line_order=line_order)
    _, html = animate.write(animation, drawn.svg, into, stem="numbered", name="Numbered")
    return html.as_uri(), animation


@pytest.mark.parametrize("order, arranged", [
    (None, None),                          # no order was given
    ([], None),                            # an empty one is none
    (["X", "Y"], None),                    # names no line the layout carries
    (["10", "B"], ["10", "B", "1", "2"]),  # two of four, the rest as the engine has them
    (["Z", "B", "B"], ["B", "1", "10", "2"]),  # what is unknown or repeated counts once
    (["2", "1", "10", "B"], ["2", "1", "10", "B"]),
    # Every line, in the order the engine writes an unordered map: the list is the
    # same as without an order, and is still written, because it was asked for.
    (["1", "10", "2", "B"], ["1", "10", "2", "B"]),
])
def test_the_page_is_told_of_an_arrangement_only_when_the_map_was_built_with_one(
        tmp_path, order, arranged):
    _, animation = _numbered_page(tmp_path, order)
    linear = animation.to_json()["linear"]
    if arranged is None:
        # Nothing is written, so an unordered map's page data is what it was.
        assert "arranged" not in linear, linear.get("arranged")
        assert set(linear) == {"columns", "lines", "names"}
        # What the page would otherwise have mistaken for an arrangement: the
        # engine's own order, which is not the page's A-Z.
        assert [line["label"] for line in linear["lines"]] == ENGINES
    else:
        assert linear["arranged"] == arranged
        # It is the order the layout is in, not a second opinion.
        assert [line["label"] for line in linear["lines"]] == arranged


# Opens one page in both themes and reads the row order the way a person sees it:
# the line names down the left, by their height, once the page has settled. It
# presses the Sort group's buttons through the Linear and Time views, and walks
# the Sort group by Tab for what each button is called, says and rings.
SORTING = BROWSER + r"""
const rows = page => page.evaluate(() => [...document.querySelectorAll("#linear-names text.rowname")]
  .map(t => [+t.getAttribute("y"), t.textContent]).sort((a, b) => a[0] - b[0]).map(p => p[1]));
const settle = page => page.evaluate(() => window.__present.settle());
const sortState = page => page.evaluate(() => {
  const group = document.getElementById("sort-group");
  const state = {};
  for (const b of group.querySelectorAll("button")) {
    state[b.id] = { pressed: b.getAttribute("aria-pressed"), shown: b.getClientRects().length > 0 };
  }
  const names = [...group.querySelectorAll("button")]
    .filter(b => b.getClientRects().length > 0).map(b => b.textContent.trim());
  return { group: group.getClientRects().length > 0, buttons: state, names };
});
main(async browser => {
  const out = {};
  for (const theme of ["warm-dark", "sepia"]) {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 },
                                           reducedMotion: "no-preference" });
    const page = await ctx.newPage();
    const cdp = await ctx.newCDPSession(page);
    const axNode = async expression => {
      const { result } = await cdp.send("Runtime.evaluate", { expression });
      const { nodes } = await cdp.send("Accessibility.getPartialAXTree",
                                       { objectId: result.objectId, fetchRelatives: false });
      return nodes[0];
    };
    const seen = { problems: [], steps: [], present: {}, stops: [] };
    page.on("pageerror", e => seen.problems.push(e.message));
    await page.goto(job.url + "?theme=" + theme, { waitUntil: "load" });
    await ready(page);

    const snap = async name => {
      await settle(page);
      seen.steps.push({ name, rows: await rows(page), ...(await sortState(page)) });
    };
    await snap("schematic");
    await page.click("#view-linear");
    await snap("linear");
    await page.click("#view-string");
    await snap("time");
    await page.click("#sort-line");
    await snap("time, A-Z");
    await page.click("#view-linear");
    await snap("linear, A-Z");
    await page.click("#sort-size");
    await snap("linear, Stations");
    if (job.arranged) {
      await page.click("#sort-arranged");
      await snap("linear, as arranged");
      await page.click("#view-string");
      await snap("time, as arranged");
    }

    // The Sort group by keyboard, from the control before it: what each button
    // is, is called, shows and rings, as the browser's own accessibility tree has it.
    await page.click("#view-linear");
    await page.focus("#labels-toggle");
    const group = await axNode("document.getElementById('sort-group')");
    seen.group = { role: group.role.value, name: group.name.value };
    for (let i = 0; i < 2 + (job.arranged ? 1 : 0); i++) {
      await page.keyboard.press("Tab");
      const info = await page.evaluate(() => {
        const el = document.activeElement, cs = getComputedStyle(el);
        return { id: el.id, text: el.textContent.trim(),
                 outline: [cs.outlineStyle, cs.outlineWidth, cs.outlineColor] };
      });
      const node = await axNode("document.activeElement");
      info.role = node.role.value;
      info.name = node.name.value.trim();
      info.props = (node.properties || []).map(p => p.name);
      seen.stops.push(info);
    }

    // What an export opens: the page in present mode, a view on its address.
    for (const view of ["linear", "time"]) {
      await page.goto(job.url + "?present=1&theme=" + theme + "&view=" + view, { waitUntil: "load" });
      await ready(page);
      await settle(page);
      seen.present[view] = await rows(page);
    }
    // A phone. A coarse pointer gives every button display:inline-flex, which beats
    // the [hidden] a browser would otherwise obey, so what the page means to hide has
    // to be hidden by a rule that outranks it. The panel starts closed on a screen this narrow.
    const phone = await browser.newContext({ viewport: { width: 390, height: 844 },
                                             hasTouch: true });
    const small = await phone.newPage();
    small.on("pageerror", e => seen.problems.push(e.message));
    await small.goto(job.url + "?theme=" + theme, { waitUntil: "load" });
    await ready(small);
    await small.evaluate(() => {
      document.getElementById("view-linear").click();
      const more = document.getElementById("more");
      if (more.getAttribute("aria-expanded") === "false") more.click();
    });
    await settle(small);
    seen.touch = { coarse: await small.evaluate(() => matchMedia("(pointer: coarse)").matches),
                   ...(await sortState(small)) };
    await phone.close();
    out[theme] = seen;
    await ctx.close();
  }
  return out;
}).catch(fail);
"""


def _sorted_rows(tmp_path, order):
    url, _ = _numbered_page(tmp_path, order)
    seen = _run(SORTING, {"url": url, "arranged": order is not None})
    tokens = _tokens(PAGE.read_text(encoding="utf-8"))
    for theme, run in seen.items():
        assert not run["problems"], (theme, run["problems"])
        # Whatever the order, a button the page shows is one a person can name,
        # press and see: a role, a name that is its text, a state, the page's ring.
        for stop in run["stops"]:
            assert (stop["role"], stop["name"]) == ("button", stop["text"]), (theme, stop)
            assert "pressed" in stop["props"], (theme, stop)
            assert tuple(stop["outline"]) == ("solid", "2px", _rgb(tokens[theme]["focus"])), (
                theme, stop["id"], stop["outline"])
        assert run["group"] == {"role": "group", "name": "Sort"}, run["group"]
    return seen


@needs_browser
def test_a_map_built_with_an_order_lists_its_rows_as_arranged_and_the_other_sorts_still_work(tmp_path):
    order = ["B"]
    arranged = ["B", "1", "10", "2"]      # the one named, then the engine's own order
    assert arranged not in (A_TO_Z, BY_SIZE, ENGINES), "the test's lines no longer tell the orders apart"
    for theme, run in _sorted_rows(tmp_path, order).items():
        steps = {s["name"]: s for s in run["steps"]}
        pressed = lambda s: {k: v["pressed"] for k, v in s["buttons"].items()}
        as_arranged = {"sort-arranged": "true", "sort-line": "false", "sort-size": "false"}

        # On a view change, and in every view the page has: the arrangement, with the
        # third button there, pressed and visible. In the schematic nothing is sorted
        # and the whole group is out of sight, as it always was.
        assert not steps["schematic"]["group"]
        for name in ("linear", "time"):
            assert steps[name]["rows"] == arranged, (theme, name, steps[name]["rows"])
            assert steps[name]["group"] and pressed(steps[name]) == as_arranged, (theme, name)
            assert steps[name]["buttons"]["sort-arranged"]["shown"], (theme, name)
        # A-Z and Stations take over, in the view they are pressed in and the other.
        assert steps["time, A-Z"]["rows"] == A_TO_Z, steps["time, A-Z"]["rows"]
        assert steps["linear, A-Z"]["rows"] == A_TO_Z
        assert pressed(steps["linear, A-Z"]) == {"sort-arranged": "false", "sort-line": "true",
                                                 "sort-size": "false"}
        assert steps["linear, Stations"]["rows"] == BY_SIZE, steps["linear, Stations"]["rows"]
        assert pressed(steps["linear, Stations"]) == {"sort-arranged": "false", "sort-line": "false",
                                                      "sort-size": "true"}
        # And As arranged brings the arrangement back.
        for name in ("linear, as arranged", "time, as arranged"):
            assert steps[name]["rows"] == arranged, (theme, name, steps[name]["rows"])
            assert pressed(steps[name]) == as_arranged
        # An export opens the page in present mode on a view and presses nothing.
        assert run["present"] == {"linear": arranged, "time": arranged}, run["present"]
        # The Sort group by keyboard: As arranged leads, then A-Z and Stations, as named.
        assert [s["name"] for s in run["stops"]] == ["As arranged", "A–Z", "Stations"]
        # On a phone too: three buttons, the arrangement pressed.
        touch = run["touch"]
        assert touch["coarse"] and touch["group"], (theme, touch)
        assert touch["names"] == ["As arranged", "A–Z", "Stations"], (theme, touch)
        assert pressed(touch) == as_arranged, (theme, touch)


@needs_browser
def test_a_map_built_without_an_order_lists_its_rows_a_to_z_and_has_no_third_button(tmp_path):
    assert A_TO_Z != ENGINES, "the test's lines no longer tell the engine's order from A-Z"
    for theme, run in _sorted_rows(tmp_path, None).items():
        steps = {s["name"]: s for s in run["steps"]}
        pressed = lambda s: {k: v["pressed"] for k, v in s["buttons"].items()}
        a_to_z = {"sort-arranged": "false", "sort-line": "true", "sort-size": "false"}

        # The engine wrote its lines 1, 10, 2, B; the page reads them 1, 2, 10, B, as it always did.
        for name in ("linear", "time"):
            assert steps[name]["rows"] == A_TO_Z, (theme, name, steps[name]["rows"])
            assert steps[name]["group"] and pressed(steps[name]) == a_to_z, (theme, name)
            # The third button is not on show, and is not what is pressed.
            assert not steps[name]["buttons"]["sort-arranged"]["shown"], (theme, name)
        assert steps["linear, Stations"]["rows"] == BY_SIZE
        assert steps["linear, A-Z"]["rows"] == A_TO_Z
        assert run["present"] == {"linear": A_TO_Z, "time": A_TO_Z}, run["present"]
        # The Sort group is what it was: two buttons, with the names they had.
        assert [s["name"] for s in run["stops"]] == ["A–Z", "Stations"]
        # On a phone as well, where a coarse pointer would show a button that is only hidden.
        touch = run["touch"]
        assert touch["coarse"], "the phone the test opens is not a coarse pointer"
        assert touch["group"] and touch["names"] == ["A–Z", "Stations"], (theme, touch)
        assert not touch["buttons"]["sort-arranged"]["shown"], (theme, touch)
        assert pressed(touch) == a_to_z, (theme, touch)


# --------------------------------------------- what the header hides, on touch
#
# Issue 64. `hidden` is the browser's own `display: none`, and the touch rule gives
# every button a display of its own, which wins: on a phone the Time view kept a
# Labels button that does nothing there. One rule outranks it for the whole header.

HIDDEN = BROWSER + r"""
main(async browser => {
  const out = {};
  const read = page => page.evaluate(() => {
    const el = document.getElementById("labels-toggle");
    return { hidden: el.hidden, display: getComputedStyle(el).display,
             parentless: el.offsetParent === null, rects: el.getClientRects().length,
             coarse: matchMedia("(pointer: coarse)").matches,
             more: document.getElementById("more").getAttribute("aria-expanded") };
  });
  const contexts = {
    fine: { viewport: { width: 1280, height: 800 } },
    coarse: { viewport: { width: 390, height: 844 }, hasTouch: true },
  };
  for (const [pointer, options] of Object.entries(contexts)) {
    const ctx = await browser.newContext({ ...options, reducedMotion: "no-preference" });
    const page = await ctx.newPage();
    const seen = { problems: [], views: {} };
    page.on("pageerror", e => seen.problems.push(e.message));
    await page.goto(job.url, { waitUntil: "load" });
    await ready(page);
    // The panel starts closed on a phone, and a button in a closed panel is not
    // rendered in any view, which would prove nothing about this one.
    await page.evaluate(() => {
      const more = document.getElementById("more");
      if (more.getAttribute("aria-expanded") === "false") more.click();
    });
    for (const view of ["map", "string", "linear", "string", "map"]) {
      await page.evaluate(v => {
        document.getElementById("view-" + v).click();
        window.__present.settle();
      }, view);
      seen.views[view] = await read(page);
    }
    out[pointer] = seen;
    await ctx.close();
  }
  return out;
}).catch(fail);
"""


@needs_browser
def test_the_labels_button_is_not_rendered_in_the_time_view_on_a_fine_pointer_or_a_coarse_one(
        tmp_path):
    seen = _run(HIDDEN, {"url": _hand_made_page(tmp_path)})
    for pointer, coarse in (("fine", False), ("coarse", True)):
        run = seen[pointer]
        assert not run["problems"], (pointer, run["problems"])
        for view, state in run["views"].items():
            assert state["coarse"] is coarse, f"the {pointer} context is not one: {state}"
            assert state["more"] == "true", (pointer, view, state)
            if view == "string":
                # The chart has its own axis and no station names to switch off.
                assert state["hidden"], (pointer, state)
                assert state["display"] == "none", (pointer, state)
                assert state["parentless"] and state["rects"] == 0, (pointer, state)
            else:
                # Everywhere else it is a button a person can see and press, as it was.
                assert not state["hidden"], (pointer, view, state)
                assert state["display"] != "none", (pointer, view, state)
                assert not state["parentless"] and state["rects"] > 0, (pointer, view, state)


def test_one_rule_hides_whatever_button_the_header_hides():
    """A rule for each button is a rule forgotten for the next one: this is the
    second time the touch rule's display has shown what the page hid."""
    page = re.sub(r"\s+", " ", PAGE.read_text(encoding="utf-8"))
    assert "header button[hidden] { display: none; }" in page


# ---------------------------------------------------------------- service days

# Two presets, so no test reads the person's own feeds: site.export() is given
# its keys, never feeds.all().
LA, CDMX = "la-metro-rail", "cdmx-metro"


class FakeBuild:
    """``pipeline.run`` stood in for, with the day the real one would choose.

    ``today`` is what the pipeline's busiest weekday would be if asked now: it
    answers it when no date is passed, as ``pipeline.run`` does, and moves
    when a test says a month has gone by.
    """

    def __init__(self, today: dt.date):
        self.today = today
        self.calls: list[tuple[str, dt.date | None]] = []
        # One trip on any day, so a page is not empty. A test that wants the
        # feed to have stopped covering a day empties it.
        self.trips: list = [SimpleNamespace()]

    def run(self, key, *, date=None, **_):
        self.calls.append((key, date))
        return SimpleNamespace(date=date or self.today, trips=list(self.trips),
                               graph=SimpleNamespace(stations=[], labels=[]))

    def dates_asked(self) -> dict[str, dt.date | None]:
        return dict(self.calls)


@pytest.fixture
def build(monkeypatch, tmp_path):
    """site.export() with the pipeline stood in for and its folders in tmp_path.

    The service-days file is read from DATA_DIR at the moment it is asked, so
    pointing DATA_DIR at tmp_path moves it too and the committed file is never
    touched.
    """
    fake = FakeBuild(dt.date(2026, 10, 7))
    monkeypatch.setattr(site, "MAPS_DIR", tmp_path / "maps")
    monkeypatch.setattr(site, "DATA_DIR", tmp_path / "_data")
    monkeypatch.setattr(pipeline, "run", fake.run)
    monkeypatch.setattr(pipeline, "stored", lambda key: None)
    monkeypatch.setattr(site, "export_comparison", lambda key: None)
    monkeypatch.setattr(site, "og_card", lambda: None)
    monkeypatch.setattr(site, "_caveats", lambda result: [])
    monkeypatch.setattr(site, "_issue_score", lambda result: 0.0)
    return fake


def stored_days() -> str:
    return site.service_days_file().read_text(encoding="utf-8")


def store_days(days: dict[str, str]) -> None:
    site.DATA_DIR.mkdir(parents=True, exist_ok=True)
    site.service_days_file().write_text(json.dumps(days, indent=2) + "\n", encoding="utf-8")


def test_a_stored_service_day_is_the_one_the_pipeline_is_given(build):
    """The day a page shows is what the file says, not what today would choose."""
    store_days({LA: "2026-09-09"})
    before = stored_days()

    entries = site.export([LA])

    assert build.dates_asked() == {LA: dt.date(2026, 9, 9)}
    assert entries[0].date == "2026-09-09"
    assert stored_days() == before, "reading a day must not rewrite the file"


def test_a_network_with_no_stored_day_is_written_once_it_is_built(build):
    """The first build chooses from today and writes the choice down."""
    entries = site.export([LA])

    assert build.dates_asked() == {LA: None}, "no stored day: the pipeline chooses"
    assert entries[0].date == "2026-10-07"
    assert site.service_days_file().exists(), "the day the pipeline chose was not written"
    assert json.loads(stored_days()) == {LA: "2026-10-07"}


def test_a_day_is_written_as_soon_as_its_network_is_built(build, monkeypatch):
    """A build that stops part way keeps the days it had already chosen."""
    def stops_at_the_second(key, *, date=None, **_):
        if key == CDMX:
            raise RuntimeError("the second network failed")
        return build.run(key, date=date)

    monkeypatch.setattr(pipeline, "run", stops_at_the_second)
    with pytest.raises(RuntimeError):
        site.export([LA, CDMX])

    assert json.loads(stored_days()) == {LA: "2026-10-07"}


def test_two_builds_a_month_apart_publish_the_same_days(build):
    """The point of the file: what the clock says no longer reaches a page."""
    first = site.export([LA, CDMX])
    build.today = dt.date(2026, 11, 7)
    second = site.export([LA, CDMX])

    assert {e.key: e.date for e in first} == {e.key: e.date for e in second}
    assert {e.date for e in second} == {"2026-10-07"}
    assert build.calls[-2:] == [(LA, dt.date(2026, 10, 7)), (CDMX, dt.date(2026, 10, 7))]


def test_the_file_is_sorted_by_key_with_one_entry_to_a_line(build):
    """So a diff of it is one network a line, whatever order they were built in."""
    store_days({"zz-added": "2026-08-12"})
    site.export([LA, CDMX])

    assert stored_days() == (
        "{\n"
        f'  "{CDMX}": "2026-10-07",\n'
        f'  "{LA}": "2026-10-07",\n'
        '  "zz-added": "2026-08-12"\n'
        "}\n"), "a network the build was not asked for keeps its day"


def test_redate_clears_one_network_and_no_other(build):
    """The next build chooses that one again and leaves the rest where they were."""
    store_days({LA: "2026-09-09", CDMX: "2026-09-10", "zz-added": "2026-08-12"})

    assert site.redate([CDMX]) == {CDMX: dt.date(2026, 9, 10)}
    assert json.loads(stored_days()) == {LA: "2026-09-09", "zz-added": "2026-08-12"}

    entries = {e.key: e.date for e in site.export([LA, CDMX])}

    assert build.dates_asked() == {LA: dt.date(2026, 9, 9), CDMX: None}
    assert entries == {LA: "2026-09-09", CDMX: "2026-10-07"}
    assert json.loads(stored_days()) == {
        LA: "2026-09-09", CDMX: "2026-10-07", "zz-added": "2026-08-12"}


def test_redate_refuses_a_key_it_cannot_find_and_changes_nothing(build):
    """A mistyped key that cleared nothing would look like a redate that worked."""
    store_days({LA: "2026-09-09"})
    before = stored_days()

    with pytest.raises(feeds.FeedError, match="no-such-network"):
        site.redate([CDMX, "no-such-network"])
    assert stored_days() == before

    # A registered network that has no day yet is not an error; there is just
    # nothing to forget, and the file is left as it was.
    assert site.redate([CDMX]) == {CDMX: None}
    assert stored_days() == before


def test_a_service_days_file_that_cannot_be_read_is_refused_by_name(build):
    """Read as empty it would have every network chosen again, silently.

    An empty file is what a write cut short leaves behind, so it is refused
    like the rest; only a file that is not there is a site never built.
    """
    for text in ("", "\n", "{", "[]", '{"la-metro-rail": "last tuesday"}',
                 '{"la-metro-rail": 3}'):
        site.DATA_DIR.mkdir(parents=True, exist_ok=True)
        site.service_days_file().write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="service-days.json"):
            site.export([LA])
    assert build.calls == [], "nothing was built from a file that could not be read"

    site.service_days_file().unlink()
    assert site.read_service_days() == {}, "no file is a site that has not been built yet"


def test_the_committed_file_is_one_the_build_can_read():
    """The file the repository carries parses, and is in the shape a build writes it.

    So the first build after a clone changes it only by adding a network, and
    a hand edit that broke the order or the layout is seen here and not as a
    diff nobody can read.
    """
    path = site.SRC_DIR / "_data" / "service-days.json"
    assert path.exists(), "service-days.json is committed with the site's data"
    days = site.read_service_days()
    stored = {key: days[key].isoformat() for key in sorted(days)}
    assert path.read_text(encoding="utf-8") == json.dumps(stored, indent=2) + "\n"


def test_the_stored_days_are_exactly_the_registrys_networks():
    """No network without a day, no day for a network that is not there.

    Read from the registry's presets and not from the disk, so a person's own
    feeds and whatever was last built cannot change the answer. A preset
    added without its day would be chosen from the clock at the next build and
    published on a day nobody looked at; a day left behind for a preset that
    was removed is a record of a page that no longer exists.
    """
    stored = set(site.read_service_days())
    assert not set(feeds.FEEDS) - stored, "no stored service day for these networks"
    assert not stored - set(feeds.FEEDS), "a stored service day for a network that is gone"


def test_a_stored_day_the_feed_no_longer_covers_is_refused(build):
    """An empty page would be built, and the atlas would rank it the cleanest."""
    store_days({LA: "2026-09-09"})
    before = stored_days()
    build.trips = []

    with pytest.raises(ValueError, match=rf"{LA}: no trips run on 2026-09-09.*--redate {LA}"):
        site.export([LA])

    assert stored_days() == before
    assert not (site.DATA_DIR / "networks.json").exists(), "an atlas of a refused build"

    # A day the pipeline chose itself is another case. It is the busiest day
    # the calendar names, so a redate would choose it again; the feed has no
    # timed trips, and the sentence must not send the person to a redate.
    # And a network built for the first time is not written down on the way.
    site.service_days_file().unlink()
    with pytest.raises(ValueError, match=rf"{LA}: the feed has no timed trips on "
                                         rf"2026-10-07, the busiest day") as refusal:
        site.export([LA])
    assert "--redate" not in str(refusal.value)
    assert "not a stored service day" in str(refusal.value)
    assert not site.service_days_file().exists()


def test_a_write_cut_short_leaves_the_file_as_it_was(build, monkeypatch):
    """The file is written beside itself and moved into place.

    A write that truncated the file first would leave it empty if the build
    were killed between the two, and the days it holds would be gone.
    """
    store_days({LA: "2026-09-09"})
    before = stored_days()

    def cut_short(src, dst):
        raise OSError("stopped before the move")

    monkeypatch.setattr(site.os, "replace", cut_short)
    with pytest.raises(OSError, match="stopped"):
        site.write_service_days({LA: dt.date(2026, 10, 7), CDMX: dt.date(2026, 10, 7)})

    assert stored_days() == before
    # Git does not ignore it and nothing else would remove it.
    assert [f.name for f in site.DATA_DIR.iterdir()] == ["service-days.json"]


# A label's halo in present mode (issue 56). The renderer writes the halo of a
# label that crosses a line as a stroke in the map's own ground,
# stroke="var(--map-bg, ...)", painted behind the glyphs. Present mode sets
# --map-bg to transparent so the map sits on the page's ground, and the stroke
# went with it: a halo painted in nothing. The page now strokes it in --bg, the
# ground the map sits on there, and only there.
#
# The hand-made network above places every label clear of its lines, and a halo
# is what a label gets when it cannot be. This one is crowded on purpose, six
# stations and five lines in a box where the label of Station 5 has no clean
# slot, and the test below asserts that it comes out haloed, so a change to the
# placement that finds it a slot fails there and not by leaving the browser test
# with nothing to look at.
CROWDED_STATIONS = {"n0": (0, 40), "n1": (50, 60), "n5": (20, 50), "n9": (60, 30),
                    "n10": (30, 60), "n11": (0, 20)}
CROWDED_EDGES = [("n5", "n10", "A"), ("n0", "n10", "B"), ("n5", "n0", "C"),
                 ("n0", "n1", "C"), ("n10", "n11", "D"), ("n1", "n9", "E")]
CROWDED_COLOURS = {"A": "0072bc", "B": "e4002b", "C": "00a651", "D": "ffd700", "E": "7b3f98"}
# The same stations nudged, standing for the network before octi straightened
# it, so the page has the geographic view and its button.
CROWDED_NUDGED = {"n0": (3, 38), "n1": (47, 63), "n5": (24, 46), "n9": (58, 34),
                  "n10": (33, 58), "n11": (-3, 22)}


def _crowded_graph(at: dict[str, tuple[float, float]]) -> LineGraph:
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": list(xy)},
              "properties": {"id": n, "station_id": "S" + n[1:],
                             "station_label": f"Station Name Number {n[1:]}"}}
             for n, xy in at.items()]
    for a, b, line in CROWDED_EDGES:
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [list(at[a]), list(at[b])]},
                      "properties": {"from": a, "to": b,
                                     "lines": [{"id": line, "label": line,
                                                "color": CROWDED_COLOURS[line]}]}})
    return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})


def _crowded_page(into: Path) -> tuple[str, str]:
    """The crowded network's page, drawn the way the pipeline draws it (themed),
    and the SVG it was given."""
    network = _crowded_graph(CROWDED_STATIONS)
    drawn = render(network, title="Crowded", style=Style(themed=True))
    geo = animate.geographic_tracks(_crowded_graph(CROWDED_NUDGED), network, drawn)

    seven = 7 * 3600
    runs = [("a", "A", ["n5", "n10"]), ("b", "B", ["n0", "n10"]),
            ("c", "C", ["n5", "n0", "n1"]), ("d", "D", ["n10", "n11"]),
            ("e", "E", ["n1", "n9"])]
    trips = [Trip(trip_id, line, "end",
                  [Call("S" + n[1:], n, seven - 60 + i * 120, seven - 40 + i * 120)
                   for i, n in enumerate(nodes)])
             for trip_id, line, nodes in runs]
    animation = animate.build(drawn, network, trips, dt.date(2026, 9, 10), geo=geo)
    assert animation.geo
    _, html = animate.write(animation, drawn.svg, into, stem="crowded", name="Crowded")
    return html.as_uri(), drawn.svg


def _haloed(svg: str) -> list[str]:
    """The opening tags of the labels the renderer wrote with a halo."""
    labels = svg[svg.index('<g id="labels"'):svg.index('<g id="trains"')]
    return re.findall(r'<text [^>]*paint-order="stroke"[^>]*>', labels)


def test_the_crowded_map_has_a_label_with_a_halo_in_the_card_ground(tmp_path):
    _, svg = _crowded_page(tmp_path)
    haloed = _haloed(svg)
    assert haloed, "no label of the crowded map came out haloed"
    # The SVG's own bytes are what the renderer has always written; the page's
    # rule changes what the browser paints, never what is written.
    for tag in haloed:
        assert 'stroke="var(--map-bg, #ffffff)"' in tag, tag
        assert 'stroke-width="3.2"' in tag, tag


HALO = BROWSER + r"""
main(async browser => {
  const out = {};
  for (const theme of ["warm-dark", "sepia"]) {
    for (const present of [true, false]) {
      const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 },
                                             reducedMotion: "no-preference" });
      const page = await ctx.newPage();
      const problems = [];
      page.on("pageerror", e => problems.push(e.message));
      await page.goto(job.url + "?" + (present ? "present=1&" : "") + "theme=" + theme,
                      { waitUntil: "load" });
      await ready(page);
      // Chosen by the attribute the renderer writes on a halo and not by the
      // page's rule, so a label the rule does not reach is still counted.
      const seen = await page.evaluate(() => ({
        present: document.documentElement.hasAttribute("data-present"),
        haloed: [...document.querySelectorAll('#labels text[paint-order="stroke"]')].map(t => {
          const cs = getComputedStyle(t);
          return { text: t.textContent, attr: t.getAttribute("stroke"), stroke: cs.stroke,
                   width: cs.strokeWidth, order: cs.paintOrder };
        }),
      }));
      out[theme + (present ? " present" : " plain")] = { ...seen, problems };
      await ctx.close();
    }
  }
  return out;
}).catch(fail);
"""


@needs_browser
def test_a_haloed_label_is_stroked_in_the_ground_the_map_sits_on_in_present_mode(tmp_path):
    url, svg = _crowded_page(tmp_path)
    tokens = _tokens(PAGE.read_text(encoding="utf-8"))
    written = len(_haloed(svg))
    assert written >= 1
    seen = _run(HALO, {"url": url})
    for theme in ("warm-dark", "sepia"):
        # Present mode: the page's ground, as the rgb() the browser computes.
        present = seen[theme + " present"]
        assert present["present"] and not present["problems"], present
        assert len(present["haloed"]) == written, present
        for label in present["haloed"]:
            assert label["stroke"] == _rgb(tokens[theme]["bg"]), (theme, label)
            # The rule changes the colour of the stroke and nothing about it.
            assert label["width"] == "3.2px" and label["order"] == "stroke", label
            # The SVG still carries the card's variable; the page's rule won.
            assert label["attr"].startswith("var(--map-bg"), label

        # Outside present mode nothing moved: the card ground, as it was. (The
        # sepia card is the page's ground, so only the warm dark can tell the
        # two apart, and only if the rule leaked would it differ.)
        plain = seen[theme + " plain"]
        assert not plain["present"] and not plain["problems"], plain
        assert len(plain["haloed"]) == written, plain
        for label in plain["haloed"]:
            assert label["stroke"] == _rgb(tokens[theme]["map-bg"]), (theme, label)
            assert label["width"] == "3.2px" and label["order"] == "stroke", label


# The halo the renderer wrote on a label is not drawn from the SVG's own text:
# the page hides that group once its script has drawn labels of its own
# (#linear-labels), and those had no halo, so the one thing the halo is for was
# dropped on the page, in every mode, when the linear view took the labels over
# (issue 56 as first written found the stroke transparent in present mode and
# missed that it was never painted). The page marks the label it draws `haloed`
# while the schematic map is what shows and strokes it as the renderer wrote it.
# Only the schematic map: the renderer solved the halo against that placement,
# the geographic map has carried the label off its slot, and beside a row there
# is no line for it to cross.
VISIBLE = BROWSER + r"""
main(async browser => {
  const out = {};
  for (const theme of ["warm-dark", "sepia"]) {
    for (const present of [true, false]) {
      const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 },
                                             reducedMotion: "no-preference" });
      const page = await ctx.newPage();
      const problems = [];
      page.on("pageerror", e => problems.push(e.message));
      await page.goto(job.url + "?" + (present ? "present=1&" : "") + "theme=" + theme,
                      { waitUntil: "load" });
      await ready(page);
      const show = async name => {
        await page.evaluate(n => { window.__present.showView(n, 0); window.__present.settle(); },
                            name);
        await frames(page, 2);
      };
      // What an element draws now, by the same element every time it is asked.
      const look = el => page.evaluate(e => {
        const cs = getComputedStyle(e);
        return { text: e.textContent, haloed: e.classList.contains("haloed"), stroke: cs.stroke,
                 width: cs.strokeWidth, order: cs.paintOrder, join: cs.strokeLinejoin,
                 opacity: cs.opacity, display: cs.display };
      }, el);
      // Every label the page draws: how many carry the class, and how many that
      // do not carry a stroke all the same.
      const census = () => page.evaluate(() => {
        const all = [...document.querySelectorAll("#linear-labels text")];
        const on = all.filter(t => t.classList.contains("haloed"));
        return { total: all.length, haloed: on.length,
                 strayStrokes: all.filter(t => !t.classList.contains("haloed")
                                              && getComputedStyle(t).stroke !== "none").length };
      });
      const seen = { present: await page.evaluate(
                       () => document.documentElement.hasAttribute("data-present")),
                     geo: await page.evaluate(() => window.__present.hasGeo()),
                     problems, views: {} };
      await show("schematic");
      const handle = await page.evaluateHandle(
        () => document.querySelector("#linear-labels text.haloed"));
      const el = handle.asElement();
      for (const name of ["schematic", "geographic", "linear", "time", "schematic"]) {
        await show(name);
        const key = name + (name === "schematic" && seen.views.time ? " again" : "");
        seen.views[key] = { label: el ? await look(el) : null, census: await census() };
      }
      out[theme + (present ? " present" : " plain")] = seen;
      await ctx.close();
    }
  }
  return out;
}).catch(fail);
"""


@needs_browser
def test_the_label_that_shows_on_the_schematic_map_keeps_the_halo_the_renderer_gave_it(tmp_path):
    url, svg = _crowded_page(tmp_path)
    tokens = _tokens(PAGE.read_text(encoding="utf-8"))
    written = len(_haloed(svg))
    assert written >= 1
    seen = _run(VISIBLE, {"url": url})
    for theme in ("warm-dark", "sepia"):
        for mode in ("present", "plain"):
            run = seen[f"{theme} {mode}"]
            assert run["present"] == (mode == "present") and run["geo"] and not run["problems"], run
            # Present mode strokes it in the ground the map sits on; the page
            # outside it, in the card the map is drawn on, as the renderer meant.
            ground = _rgb(tokens[theme]["bg" if mode == "present" else "map-bg"])

            for view in ("schematic", "schematic again"):
                shown = run["views"][view]
                label = shown["label"]
                assert label, f"{theme} {mode} {view}: no label carries the halo"
                assert label["haloed"] and label["stroke"] == ground, (theme, mode, view, label)
                assert label["width"] == "3.2px" and label["order"] == "stroke", label
                assert label["join"] == "round" and label["display"] != "none", label
                assert label["opacity"] == "1", label
                # One label, and no other label gains a stroke from it.
                assert shown["census"]["haloed"] == written, shown
                assert shown["census"]["strayStrokes"] == 0, shown

            # The same element, on every other view: no class, no stroke.
            for view in ("geographic", "linear", "time"):
                shown = run["views"][view]
                label = shown["label"]
                assert not label["haloed"] and label["stroke"] == "none", (theme, mode, view, label)
                assert shown["census"]["haloed"] == 0 and shown["census"]["strayStrokes"] == 0, shown
