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


def test_agency_filter_cascades_through_references(tmp_path):
    """Dropping routes without their trips and stop_times leaves orphans, which
    LOOM rejects -- and Mexico City needs the filter because Suburbano also has
    a line numbered 1 at route_type 1."""
    import io
    import zipfile

    src = tmp_path / "feed.zip"
    tables = {
        "agency.txt": "agency_id,agency_name\nMETRO,Metro\nOTHER,Suburbano\n",
        "routes.txt": ("route_id,agency_id,route_short_name,route_long_name,route_type\n"
                       "R1,METRO,1,Linea 1,1\nR2,OTHER,1,Suburbano,1\n"),
        "trips.txt": "route_id,service_id,trip_id\nR1,s,T1\nR2,s,T2\n",
        "stop_times.txt": ("trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                           "T1,00:00:00,00:00:00,A,1\nT2,00:00:00,00:00:00,B,1\n"),
        "frequencies.txt": "trip_id,start_time,end_time,headway_secs\nT1,05:00:00,06:00:00,300\nT2,05:00:00,06:00:00,300\n",
        "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nA,Alpha,19.4,-99.1\nB,Beta,19.5,-99.2\n",
    }
    with zipfile.ZipFile(src, "w") as z:
        for name, body in tables.items():
            z.writestr(name, body)

    feed = feeds.Feed(key="t", name="T", url="http://x", agency="METRO")
    feeds.FEEDS["t"] = feed
    try:
        object.__setattr__(feed, "key", "t")
        # Point the cache at the fixture rather than downloading.
        original = feeds.fetch
        feeds.fetch = lambda key, force=False: src
        out = feeds.normalize("t", force=True)
        with zipfile.ZipFile(out) as z:
            read = lambda n: pd.read_csv(io.BytesIO(z.read(n)), dtype=str)
            assert list(read("routes.txt")["route_id"]) == ["R1"]
            assert list(read("trips.txt")["trip_id"]) == ["T1"]
            assert list(read("stop_times.txt")["trip_id"]) == ["T1"]
            assert list(read("frequencies.txt")["trip_id"]) == ["T1"]
    finally:
        feeds.fetch = original
        feeds.FEEDS.pop("t", None)
