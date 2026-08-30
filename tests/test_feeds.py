"""The feed registry is the one place a new city is added, so keep it coherent."""

import re

import pandas as pd
import pytest

from schematic import feeds

# LOOM's -m accepts these names or raw GTFS route-type codes, comma separated.
VALID_MOTS = {"all", "tram", "streetcar", "subway", "metro", "rail", "train", "bus",
              "ferry", "boat", "ship", "cablecar", "gondola", "funicular", "coach",
              "mono-rail", "monorail", "trolley", "trolleybus", "trolley-bus"}


@pytest.mark.parametrize("key,feed", sorted(feeds.FEEDS.items()))
def test_registry_entry_is_wellformed(key, feed):
    assert feed.key == key, "the dict key and Feed.key must agree"
    assert feed.name
    assert feed.url.startswith("http")
    for mot in feed.mode.split(","):
        mot = mot.strip()
        assert mot in VALID_MOTS or mot.isdigit(), f"{key}: unknown MOT {mot!r}"
    for pattern in (feed.label_pattern, feed.label_strip):
        if pattern:
            re.compile(pattern)  # raises if malformed


def test_keys_are_url_safe():
    """Keys become output filenames, so they must not need escaping."""
    for key in feeds.FEEDS:
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", key), key


def test_no_duplicate_urls():
    urls = [f.url for f in feeds.FEEDS.values()]
    assert len(urls) == len(set(urls))


def test_label_derivation():
    """route_short_name wins; otherwise derive from the long name, then strip."""
    routes = pd.DataFrame([
        {"route_id": "801", "route_short_name": None, "route_long_name": "Metro A Line"},
        {"route_id": "802", "route_short_name": "  ", "route_long_name": "Metro B Line"},
        {"route_id": "803", "route_short_name": "K", "route_long_name": "Metro K Line"},
        {"route_id": "804", "route_short_name": None, "route_long_name": "Something Else"},
        {"route_id": "805", "route_short_name": None, "route_long_name": None},
    ])
    assert list(feeds.route_labels("la-metro-rail", routes)) == [
        "A", "B", "K", "Something Else", "805"]


def test_label_strip_merges_directional_variants():
    routes = pd.DataFrame([
        {"route_id": "1", "route_short_name": "Yellow-N", "route_long_name": None},
        {"route_id": "2", "route_short_name": "Yellow-S", "route_long_name": None},
        {"route_id": "3", "route_short_name": "BridgeA", "route_long_name": None},
    ])
    # The two directions collapse onto one line; a name that merely ends in a
    # letter must not be truncated.
    assert list(feeds.route_labels("bart", routes)) == ["Yellow", "Yellow", "BridgeA"]
