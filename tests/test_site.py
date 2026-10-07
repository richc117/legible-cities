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
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from schematic import feeds, pipeline, site

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


def test_the_icons_are_the_four_vendored_calcite_files():
    """They ship as published; the schematic one is turned by CSS, not by hand."""
    from schematic import animate

    icons = Path(animate.__file__).parent / "page" / "icons"
    assert set(animate._VIEW_ICONS) == {
        "map-16", "code-branch-16", "connection-to-connection-16", "clock-16"}
    for name in animate._VIEW_ICONS:
        assert (icons / f"{name}.svg").exists(), name

    page = PAGE.read_text()
    for placeholder in ("__ICON_GEO__", "__ICON_SCHEMATIC__", "__ICON_LINEAR__",
                        "__ICON_TIME__"):
        assert placeholder in page, placeholder
    # The 45-degree turn is a transform over the mask, never an edit to the file.
    assert "#view-map::before { transform: rotate(90deg); }" in page


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

    def run(self, key, *, date=None, **_):
        self.calls.append((key, date))
        return SimpleNamespace(date=date or self.today, trips=[],
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
    """Read as empty it would have every network chosen again, silently."""
    for text in ("{", "[]", '{"la-metro-rail": "last tuesday"}', '{"la-metro-rail": 3}'):
        site.DATA_DIR.mkdir(parents=True, exist_ok=True)
        site.service_days_file().write_text(text, encoding="utf-8")
        with pytest.raises(ValueError, match="service-days.json"):
            site.export([LA])
    assert build.calls == [], "nothing was built from a file that could not be read"

    # An empty file, or none, is a site that has not been built yet.
    site.service_days_file().write_text("", encoding="utf-8")
    assert site.read_service_days() == {}
    site.service_days_file().unlink()
    assert site.read_service_days() == {}


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
