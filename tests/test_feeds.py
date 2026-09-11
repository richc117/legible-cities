"""The feed registry is the one place a new city is added, so keep it coherent."""

import re

import pandas as pd
import pytest

from schematic import feeds

# LOOM's -m accepts these names or raw GTFS route-type codes, comma separated.
VALID_MOTS = feeds.MOTS


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


# ------------------------------------------------------------ the user half
#
# Feeds a person added: kept beside the zips, checked before they are kept,
# and gone with their files when removed. Nothing here touches the network:
# a URL is answered by a stand-in for requests.get.

import json
import os
import subprocess
import sys

from schematic import config

GOOD = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n"
                  "M,Metro de Prueba,http://x,America/Mexico_City\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\nA,Alpha,19.4,-99.1\nB,Beta,19.5,-99.2\n",
    "routes.txt": "route_id,agency_id,route_short_name,route_long_name,route_type\nR1,M,1,Linea 1,1\n",
    "trips.txt": "route_id,service_id,trip_id\nR1,s,T1\n",
    "stop_times.txt": "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                      "T1,06:00:00,06:00:00,A,1\nT1,06:10:00,06:10:00,B,2\n",
    "calendar.txt": "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                    "start_date,end_date\ns,1,1,1,1,1,0,0,20260101,20261231\n",
}


def gtfs_zip(path, tables=GOOD, *, folder=""):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        for name, body in tables.items():
            z.writestr(folder + name, body)
    return path


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV, str(tmp_path))
    return tmp_path


def test_a_feed_round_trips_through_json():
    feed = feeds.get("la-metro-rail")
    again = feeds.Feed.from_dict(json.loads(json.dumps(feed.to_dict())))
    assert again == feed
    # A record from a newer engine, with a field this one does not know, still reads.
    assert feeds.Feed.from_dict({**feed.to_dict(), "future": 1}) == feed
    with pytest.raises(feeds.FeedError, match="needs a url"):
        feeds.Feed.from_dict({"key": "x", "name": "X"})


def test_the_presets_are_the_registry_when_nothing_was_added(home):
    assert feeds.all() == feeds.FEEDS
    assert len(feeds.FEEDS) == 22
    assert all(f.source == "preset" for f in feeds.FEEDS.values())
    with pytest.raises(feeds.FeedError, match="not a registered feed"):
        feeds.get("nowhere")


def test_add_from_a_file_names_keys_and_keeps_the_feed(home, tmp_path):
    src = gtfs_zip(tmp_path / "download.zip")
    feed = feeds.add(src)
    assert feed.key == "metro-de-prueba"
    assert feed.name == "Metro de Prueba"
    assert feed.source == "user"
    assert feed.url == ""
    assert feed.zip_path.is_file()
    assert feeds.get(feed.key) == feed
    assert feeds.all()[feed.key] == feed
    assert set(feeds.all()) == set(feeds.FEEDS) | {feed.key}
    # The staging copy is gone; the record is the file's whole content.
    assert sorted(p.name for p in config.feeds_dir().iterdir()) == [
        "metro-de-prueba.zip", "user-feeds.json"]
    records = json.loads(feeds.user_file().read_text())
    assert [r["key"] for r in records] == ["metro-de-prueba"]
    # The tables read through the same path a preset's do.
    assert set(feeds.tables(feed.key)) >= {"stops", "routes", "trips", "stop_times"}


def test_a_user_feed_survives_a_new_process(home, tmp_path):
    feed = feeds.add(gtfs_zip(tmp_path / "f.zip"), key="mine", name="Mine")
    out = subprocess.run(
        [sys.executable, "-c",
         "from schematic import feeds; f = feeds.get('mine'); print(f.name, f.source)"],
        capture_output=True, text=True, check=True,
        env={**os.environ, config.ENV: str(home)}).stdout
    assert out.strip() == "Mine user"
    assert feed.source == "user"


def test_add_from_a_url_downloads_once_and_keeps_the_url(home, tmp_path, monkeypatch):
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()
    calls = []

    class Response:
        content = payload

        def __init__(self):
            self.headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for i in range(0, len(self.content), size):
                yield self.content[i:i + size]

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: calls.append(url) or Response())
    feed = feeds.add("https://example.test/gtfs.zip")
    assert feed.url == "https://example.test/gtfs.zip"
    assert calls == ["https://example.test/gtfs.zip"]
    assert feeds.fetch(feed.key) == feed.zip_path
    assert calls == ["https://example.test/gtfs.zip"], "cached; not fetched again"


def test_a_page_that_is_not_a_zip_is_refused(home, monkeypatch):
    class Response:
        content = b"<html>not found</html>"

        def __init__(self):
            self.headers = {}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield self.content

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    with pytest.raises(feeds.FeedError, match="did not return a zip"):
        feeds.add("https://example.test/oops")
    assert not feeds.user_file().exists()
    assert list(config.feeds_dir().glob("*.zip")) == []


@pytest.mark.parametrize("missing,sentence", [
    ("stop_times.txt", "has no stop_times.txt, so there is no timetable to animate"),
    ("stops.txt", "has no stops.txt, so there is no network to draw"),
    ("routes.txt", "has no routes.txt, so there is no network to draw"),
    ("trips.txt", "has no trips.txt, so there is no network to draw"),
    ("calendar.txt", "has neither calendar.txt nor calendar_dates.txt, so there is no service day"),
])
def test_a_zip_missing_a_table_is_refused_by_name_and_leaves_nothing(home, tmp_path,
                                                                    missing, sentence):
    tables = {k: v for k, v in GOOD.items() if k != missing}
    src = gtfs_zip(tmp_path / "partial.zip", tables)
    with pytest.raises(feeds.FeedError, match=sentence):
        feeds.add(src)
    assert not feeds.user_file().exists()
    assert list(config.feeds_dir().iterdir()) == []


def test_calendar_dates_alone_is_a_calendar(home, tmp_path):
    tables = {k: v for k, v in GOOD.items() if k != "calendar.txt"}
    tables["calendar_dates.txt"] = "service_id,date,exception_type\ns,20260601,1\n"
    assert feeds.add(gtfs_zip(tmp_path / "exc.zip", tables)).key == "metro-de-prueba"


def test_tables_in_a_folder_inside_the_zip_are_found(home, tmp_path):
    feed = feeds.add(gtfs_zip(tmp_path / "nested.zip", folder="gtfs/"))
    assert feed.key == "metro-de-prueba"


def test_something_that_is_not_a_zip_is_refused(home, tmp_path):
    prose = tmp_path / "readme.zip"
    prose.write_text("hello")
    with pytest.raises(feeds.FeedError, match="is not a zip file"):
        feeds.add(prose)
    with pytest.raises(feeds.FeedError, match="is not a file"):
        feeds.add(tmp_path / "missing.zip")


def test_keys_are_unique_and_a_preset_key_is_taken(home, tmp_path):
    first = feeds.add(gtfs_zip(tmp_path / "a.zip"))
    second = feeds.add(gtfs_zip(tmp_path / "b.zip"))
    assert (first.key, second.key) == ("metro-de-prueba", "metro-de-prueba-2")
    with pytest.raises(feeds.FeedError, match="already a feed"):
        feeds.add(gtfs_zip(tmp_path / "c.zip"), key="la-metro-rail")
    with pytest.raises(feeds.FeedError, match="already a feed"):
        feeds.add(gtfs_zip(tmp_path / "d.zip"), key="metro-de-prueba")
    with pytest.raises(feeds.FeedError, match="lower-case letters"):
        feeds.add(gtfs_zip(tmp_path / "e.zip"), key="Not A Key")
    with pytest.raises(feeds.FeedError, match="not a mode"):
        feeds.add(gtfs_zip(tmp_path / "f.zip"), mode="hovercraft")
    assert len(feeds.all()) == len(feeds.FEEDS) + 2


def test_a_feed_without_an_agency_name_is_named_after_its_file(home, tmp_path):
    tables = {k: v for k, v in GOOD.items() if k != "agency.txt"}
    feed = feeds.add(gtfs_zip(tmp_path / "Springfield Transit.zip", tables))
    assert (feed.name, feed.key) == ("Springfield Transit", "springfield-transit")


def test_remove_forgets_a_user_feed_with_its_files_and_refuses_a_preset(home, tmp_path):
    feed = feeds.add(gtfs_zip(tmp_path / "a.zip"), key="mine")
    normalized = feeds.normalize("mine")
    assert normalized.is_file()
    layouts = config.graphs_dir() / "mine" / ("0" * 64)
    layouts.mkdir(parents=True)
    (layouts / "03_octi.json").write_text("{}")
    other = feeds.add(gtfs_zip(tmp_path / "b.zip"), key="theirs")
    feeds.remove("mine")
    assert "mine" not in feeds.all()
    assert feeds.get("theirs") == other
    assert not feed.zip_path.exists()
    assert not normalized.exists()
    assert not (config.graphs_dir() / "mine").exists()
    assert other.zip_path.exists()
    with pytest.raises(feeds.FeedError, match="built-in"):
        feeds.remove("la-metro-rail")
    with pytest.raises(feeds.FeedError, match="not a registered feed"):
        feeds.remove("mine")
    assert len(feeds.FEEDS) == 22


def test_a_file_feed_whose_zip_is_gone_says_so(home, tmp_path):
    feed = feeds.add(gtfs_zip(tmp_path / "a.zip"), key="mine")
    feed.zip_path.unlink()
    with pytest.raises(feeds.FeedError, match="add it again"):
        feeds.fetch("mine")


def test_add_reports_the_download_and_the_check_and_stops_when_asked(home, tmp_path, monkeypatch):
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()

    class Response:
        content = payload

        def __init__(self):
            self.headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for i in range(0, len(self.content), 64):
                yield self.content[i:i + 64]

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    seen = []
    feeds.add("https://example.test/gtfs.zip", key="seen",
              progress=lambda stage, done, total: seen.append((stage, done, total)))
    downloads = [s for s in seen if s[0] == "download"]
    assert downloads[-1] == ("download", len(payload), len(payload))
    assert len(downloads) > 1
    assert seen[-1] == ("check", 1, None)

    # Cancelled after the first chunk: nothing is kept.
    asked = []
    with pytest.raises(feeds.Interrupted):
        feeds.add("https://example.test/gtfs.zip", key="gone",
                  cancelled=lambda: asked.append(1) or len(asked) > 1)
    assert "gone" not in feeds.all()
    assert sorted(p.name for p in config.feeds_dir().iterdir()) == ["seen.zip", "user-feeds.json"]


def test_an_empty_agency_means_every_operator_where_the_entry_names_one():
    cdmx = feeds.get("cdmx-metro")
    assert cdmx.agency == "METRO"
    assert feeds.resolved("cdmx-metro").agency == "METRO"
    assert feeds.resolved("cdmx-metro", agency=None).agency == "METRO"
    assert feeds.resolved("cdmx-metro", agency=feeds.NO_AGENCY).agency is None
    assert feeds.resolved("cdmx-metro", agency="SUB").agency == "SUB"

