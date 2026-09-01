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
