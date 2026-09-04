"""The site's sharing tags and the card they point at.

A link preview fails silently: the tags are either absent or relative, the page
looks perfect in a browser, and the only symptom is a bare link in somebody
else's chat window. So the things asserted here are the ones that cannot be
seen by opening the site -- that an origin exists, that both consumers of it
emit whole URLs, and that the card is the size it claims to be.
"""

import json
import re
import struct
from pathlib import Path

import pytest

from schematic import feeds, site

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

