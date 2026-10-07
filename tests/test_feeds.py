"""The feed registry is the one place a new city is added, so keep it coherent."""

import re

import pandas as pd
import pytest

from schematic import config, feeds

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


# The presets whose agency serves no TLS for the feed, so that its address
# stays plain http: key -> why, naming the month and year it was checked. The
# entry's comment in feeds.py says the same. It is empty while every agency
# serves https, and it belongs here rather than in a feed's `notes`, which a
# reader of the atlas, an export's sidecar and the app all see.
PLAIN_HTTP: dict[str, str] = {}


def _http_but_not_listed(registry, listed):
    """The keys whose address is plain http and which ``listed`` does not name."""
    return sorted(key for key, feed in registry.items()
                  if not feed.url.startswith("https://") and key not in listed)


def _listed_but_not_http(registry, listed):
    """The keys ``listed`` names that are no longer plain http, or no longer
    registered: a list that outlives the move to https says something false."""
    return sorted(key for key in listed
                  if key not in registry or registry[key].url.startswith("https://"))


def test_every_preset_is_fetched_over_https_unless_it_is_listed_as_plain_http():
    """A preset's zip is downloaded on a person's machine, and plain http
    invites tampering and proxies. A new preset cannot slip back to it
    silently: an agency that serves no TLS goes in ``PLAIN_HTTP`` with a reason."""
    assert _http_but_not_listed(feeds.FEEDS, PLAIN_HTTP) == []


def test_the_plain_http_list_holds_only_presets_that_are_still_plain_http():
    assert _listed_but_not_http(feeds.FEEDS, PLAIN_HTTP) == []


def test_the_plain_http_check_reads_both_directions():
    registry = {f.key: f for f in (
        feeds.Feed(key="bare", name="A", url="http://example.invalid/a.zip"),
        feeds.Feed(key="listed", name="B", url="http://example.invalid/b.zip"),
        feeds.Feed(key="moved", name="C", url="https://example.invalid/c.zip"),
        feeds.Feed(key="secure", name="D", url="https://example.invalid/d.zip"))}
    listed = {"listed": "serves no TLS (checked Oct 2026)",
              "moved": "serves no TLS (checked Oct 2026)",
              "gone": "serves no TLS (checked Oct 2026)"}
    assert _http_but_not_listed(registry, listed) == ["bare"]
    assert _listed_but_not_http(registry, listed) == ["gone", "moved"]


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


def test_agency_filter_cascades_through_references(home, tmp_path):
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

    # ``normalize`` writes beside the zips and leaves making that folder to
    # ``fetch``, which this test replaces.
    config.feeds_dir().mkdir(parents=True)
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


def test_a_cancel_after_the_download_leaves_no_feed(home, tmp_path, monkeypatch):
    """E23: the cancel used to be asked only between the download's chunks,
    so a yes that arrived after the last one fell through to the commit --
    the server re-checks once the work returns and answered the request with
    the cancelled error while the feed sat in the registry."""
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for i in range(0, len(payload), 64):
                yield payload[i:i + 64]

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    stages = []
    with pytest.raises(feeds.Interrupted):
        feeds.add("https://example.test/gtfs.zip", key="late",
                  progress=lambda stage, done, total: stages.append(stage),
                  # Not cancelled while the bytes arrive; cancelled once the
                  # feed has been checked, which is where the defect lived.
                  cancelled=lambda: "check" in stages)
    assert stages.count("check") == 2, "the download ran to its end and the feed was checked"
    assert "late" not in feeds.all()
    assert feeds.user_feeds() == {}
    assert not list(config.feeds_dir().glob("*.zip"))


def test_a_cancel_on_a_file_add_leaves_no_feed(home, tmp_path):
    """The same guarantee for a zip the client owns. Nothing asks the cancel
    while ``shutil.copyfile`` runs, so the one check before the commit is
    what makes this path answerable at all."""
    src = gtfs_zip(tmp_path / "local.zip")
    with pytest.raises(feeds.Interrupted):
        feeds.add(src, key="local", cancelled=lambda: True)
    assert "local" not in feeds.all()
    assert feeds.user_feeds() == {}
    assert not list(config.feeds_dir().glob("*.zip"))
    assert src.is_file(), "the source the client owns is left where it was"


def test_an_empty_agency_means_every_operator_where_the_entry_names_one():
    cdmx = feeds.get("cdmx-metro")
    assert cdmx.agency == "METRO"
    assert feeds.resolved("cdmx-metro").agency == "METRO"
    assert feeds.resolved("cdmx-metro", agency=None).agency == "METRO"
    assert feeds.resolved("cdmx-metro", agency=feeds.NO_AGENCY).agency is None
    assert feeds.resolved("cdmx-metro", agency="SUB").agency == "SUB"



# ------------------------------------------------- a preset's first download
#
# E36. A preset's zip is fetched the first time something needs it, deep in a
# layout or an inspection. Inside ``watched`` that download reports its bytes
# and stops on a cancel, leaving nothing, as a person's own feed's does.


def _preset(monkeypatch, payload: bytes, *, chunk: int = 64):
    """A preset ``t`` whose URL is answered by ``payload`` in ``chunk``s."""
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T",
                                                     url="https://example.test/t.zip"))
    gets = []

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for i in range(0, len(payload), chunk):
                yield payload[i:i + chunk]

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: gets.append(url) or Response())
    return gets


def test_a_watched_fetch_reports_its_bytes_and_a_cached_one_nothing(home, tmp_path, monkeypatch):
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()
    gets = _preset(monkeypatch, payload)
    heard = []
    with feeds.watched(lambda stage, done, total: heard.append((stage, done, total)), None):
        path = feeds.fetch("t")
    assert path.read_bytes() == payload
    assert len(heard) > 1 and all(stage == "download" for stage, _d, _t in heard)
    assert heard[-1] == ("download", len(payload), len(payload))

    heard.clear()
    with feeds.watched(lambda *a: heard.append(a), None):
        assert feeds.fetch("t") == path
    assert heard == [] and gets == ["https://example.test/t.zip"], "cached: no download, no report"


def test_a_cancelled_fetch_keeps_nothing_and_the_next_downloads_afresh(home, tmp_path, monkeypatch):
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()
    gets = _preset(monkeypatch, payload)
    asked = []
    with feeds.watched(None, lambda: asked.append(1) or len(asked) > 1):
        with pytest.raises(feeds.Interrupted):
            feeds.fetch("t")
    assert sorted(p.name for p in config.feeds_dir().iterdir()) == [], "no zip and no .part"

    # Outside a request nothing is watched, and the feed is fetched whole.
    assert feeds.fetch("t").read_bytes() == payload
    assert len(gets) == 2, "downloaded afresh"


def test_a_cancel_after_the_last_chunk_still_keeps_nothing(home, tmp_path, monkeypatch):
    """The download ran to its end and the cancel came after: whole or not at
    all, so the zip is not kept - as add() has it since E23."""
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()
    _preset(monkeypatch, payload)
    seen = []
    with feeds.watched(lambda stage, done, total: seen.append(done),
                       lambda: bool(seen) and seen[-1] == len(payload)):
        with pytest.raises(feeds.Interrupted):
            feeds.fetch("t")
    assert seen[-1] == len(payload)
    assert list(config.feeds_dir().iterdir()) == []


def test_a_download_that_fails_midway_leaves_no_part_file(home, tmp_path, monkeypatch):
    """Whatever stops a download after its first bytes - here the connection
    dropping, which is not a refusal ``_download`` cleans up itself - the
    ``.part`` goes with it (E36)."""
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T",
                                                     url="https://example.test/t.zip"))

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield payload[:64]
            raise OSError("connection reset")

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    with feeds.watched(None, None):
        with pytest.raises(OSError):
            feeds.fetch("t")
    assert list(config.feeds_dir().iterdir()) == []


def test_an_already_cancelled_request_fetches_nothing(home, tmp_path, monkeypatch):
    """A request that waited on the feed's lock while another's download was
    cancelled may have been cancelled itself: it asks before fetching a byte."""
    gets = _preset(monkeypatch, gtfs_zip(tmp_path / "remote.zip").read_bytes())
    with feeds.watched(None, lambda: True):
        with pytest.raises(feeds.Interrupted):
            feeds.fetch("t")
    assert gets == []
    assert list(config.feeds_dir().iterdir()) == []


def test_the_watcher_is_the_calling_threads_alone(home, tmp_path, monkeypatch):
    """A download on another thread, outside any request, is not reported to
    this one's watcher and ignores its cancel."""
    import threading

    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()
    _preset(monkeypatch, payload)
    heard, done = [], []
    with feeds.watched(lambda *a: heard.append(a), lambda: True):
        other = threading.Thread(target=lambda: done.append(feeds.fetch("t")))
        other.start()
        other.join(10)
    assert done and done[0].read_bytes() == payload
    assert heard == []


# -- an address in a sentence, without its secrets (issue 32) ----------------

@pytest.mark.parametrize("url,said", [
    ("https://example.test/gtfs.zip", "https://example.test/gtfs.zip"),
    ("https://example.test:8443/a/b.zip", "https://example.test:8443/a/b.zip"),
    ("https://someone:pw@feeds.example.test/gtfs.zip",
     "https://<redacted>@feeds.example.test/gtfs.zip"),
    ("https://example.test/g.zip?api_key=S3CRET&format=zip",
     "https://example.test/g.zip?api_key=<redacted>&format=<redacted>"),
    ("https://example.test/g.zip?a=1&key=S3;v=2",
     "https://example.test/g.zip?a=<redacted>&key=<redacted>;v=<redacted>"),
    ("http://example.test/f.zip?s3cr3t", "http://example.test/f.zip?<redacted>"),
    ("https://example.test/f.zip?QUJDRA==", "https://example.test/f.zip?<redacted>"),
    ("https://example.test/f.zip?empty=", "https://example.test/f.zip?empty="),
    ("https://example.test/feed#access_token=zzz", "https://example.test/feed#<redacted>"),
    ("https://example.test/feed#", "https://example.test/feed#"),
    ("http://[2001:db8::1]/g.zip?key=S3", "http://[2001:db8::1]/g.zip?key=<redacted>"),
    ("https://u:p@example.test/g.zip?k=v#f",
     "https://<redacted>@example.test/g.zip?k=<redacted>#<redacted>"),
    ("", ""),
])
def test_an_address_is_shown_without_its_secrets(url, said):
    assert feeds.shown(url) == said
    assert feeds.shown(said) == said, "a second pass changes nothing"


KEYED = "https://someone:pw@example.test/feeds/gtfs.zip?api_key=S3CRET#tok"
SECRETS = ("S3CRET", "someone", "pw@", "tok")


def _says_nothing_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} is in {text!r}"


def test_a_refused_page_names_the_address_without_its_key(home, monkeypatch):
    class Response:
        headers: dict = {}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield b"<html>not found</html>"

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    with pytest.raises(feeds.FeedError) as refused:
        feeds.add(KEYED)
    said = str(refused.value)
    assert said.startswith("https://<redacted>@example.test/feeds/gtfs.zip?api_key=<redacted>")
    assert "did not return a zip" in said
    _says_nothing_secret(said)


@pytest.mark.parametrize("raised,why", [
    (lambda: _http_error(404), "the server answered 404 Not Found"),
    (lambda: _http_error(599), "the server answered 599"),
    (lambda: feeds.requests.ConnectTimeout(f"timed out for url: {KEYED}"),
     "the server did not answer in time"),
    (lambda: feeds.requests.exceptions.SSLError(f"bad certificate for url: {KEYED}"),
     "a secure connection could not be made"),
    (lambda: feeds.requests.ConnectionError(
        "HTTPSConnectionPool(host='example.test', port=443): Max retries exceeded "
        "with url: /feeds/gtfs.zip?api_key=S3CRET"), "the server could not be reached"),
    (lambda: feeds.requests.TooManyRedirects(f"Exceeded 30 redirects for {KEYED}"),
     "it redirected too many times"),
    (lambda: feeds.requests.RequestException(f"something about {KEYED}"),
     "the request failed (RequestException)"),
])
def test_a_failed_download_says_why_in_our_words_and_carries_no_cause(home, monkeypatch,
                                                                      raised, why):
    """``requests`` and urllib3 repeat the address in their own text, so
    neither that text nor the exception itself travels with the refusal: a
    logged traceback prints a cause's message in full."""
    def get(url, **kw):
        assert url == KEYED, "the fetch itself asks for the whole address"
        raise raised()

    monkeypatch.setattr(feeds.requests, "get", get)
    with pytest.raises(feeds.FeedError) as refused:
        feeds.add(KEYED)
    said = str(refused.value)
    assert said == ("https://<redacted>@example.test/feeds/gtfs.zip?api_key=<redacted>"
                    f"#<redacted> could not be fetched: {why}")
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    import traceback
    printed = "".join(traceback.format_exception(refused.value))
    _says_nothing_secret(printed)
    assert list(config.feeds_dir().glob("*.zip")) == []


def _http_error(status: int):
    class Answer:
        status_code = status
        reason = f"nope, for url: {KEYED}"
    return feeds.requests.HTTPError(f"{status} Client Error: nope for url: {KEYED}",
                                    response=Answer())


def test_a_keyed_address_is_kept_whole_to_fetch_and_named_from_its_path(home, tmp_path,
                                                                       monkeypatch):
    """The record keeps the address as it was given, since that is what a
    later fetch asks for; the name a feed with no agency takes comes from the
    path's file, never from the query."""
    tables = {k: v for k, v in GOOD.items() if k != "agency.txt"}
    payload = gtfs_zip(tmp_path / "remote.zip", tables).read_bytes()

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield payload

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    feed = feeds.add("https://example.test/feeds/Springfield.zip?api_key=S3CRET")
    assert feed.url == "https://example.test/feeds/Springfield.zip?api_key=S3CRET"
    assert (feed.name, feed.key) == ("Springfield", "springfield")


def test_a_missing_table_names_the_address_without_its_key(home, tmp_path, monkeypatch):
    tables = {k: v for k, v in GOOD.items() if k != "stops.txt"}
    payload = gtfs_zip(tmp_path / "remote.zip", tables).read_bytes()

    class Response:
        headers: dict = {}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield payload

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    with pytest.raises(feeds.FeedError, match="has no stops.txt") as refused:
        feeds.add(KEYED)
    _says_nothing_secret(str(refused.value))


def test_a_presets_refusal_names_its_public_address_whole(home, monkeypatch):
    """A preset's address is in the registry for anyone to read, so a failed
    fetch of one names it as it is, query included; only the library's own
    text is kept out, as for every feed."""
    url = "https://example.test/t.zip?alt=media"
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T", url=url))

    def get(asked, **kw):
        raise feeds.requests.ConnectionError(f"Max retries exceeded with url: {asked}")

    monkeypatch.setattr(feeds.requests, "get", get)
    with pytest.raises(feeds.FeedError) as refused:
        feeds.fetch("t")
    assert str(refused.value) == f"{url} could not be fetched: the server could not be reached"


def test_a_persons_feed_fetched_again_names_its_address_without_its_key(home, tmp_path,
                                                                       monkeypatch):
    """A feed added from a keyed link and fetched again later - its zip gone -
    fails with the same care as the add did."""
    payload = gtfs_zip(tmp_path / "remote.zip").read_bytes()

    class Response:
        headers = {"Content-Length": str(len(payload))}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            yield payload

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    feed = feeds.add(KEYED, key="mine")
    feed.zip_path.unlink()

    def get(asked, **kw):
        assert asked == KEYED
        raise _http_error(403)

    monkeypatch.setattr(feeds.requests, "get", get)
    with pytest.raises(feeds.FeedError) as refused:
        feeds.fetch("mine")
    assert "could not be fetched: the server answered 403 Forbidden" in str(refused.value)
    _says_nothing_secret(str(refused.value))


# ------------------------------------------------------------- the headways
#
# Issue 46. A registry entry says whether its service is run from
# frequencies.txt (trains at a scheduled interval) rather than a timetable of
# trip times. A preset says so by hand; a feed a person adds is decided from
# its zip where the zip is checked, and the answer is written to its record.

# Every preset's zip was read in Oct 2026. Mexico City's frequencies.txt has
# 1,583 rows covering all 1,204 of its trips. Three others carry the file and
# are timetables all the same: Phoenix has 4 rows among 18,157 trips, and
# Chicago and Pittsburgh ship it empty. None of those three may flip.
PUBLISHES_HEADWAYS = {"cdmx-metro"}


@pytest.mark.parametrize("key", sorted(feeds.FEEDS))
def test_only_mexico_city_publishes_headways_among_the_presets(key):
    assert feeds.FEEDS[key].headways is (key in PUBLISHES_HEADWAYS)
    assert feeds.get(key).to_dict()["headways"] is (key in PUBLISHES_HEADWAYS)


def test_a_feed_publishes_no_headways_unless_it_is_said_to():
    assert feeds.Feed(key="k", name="K", url="https://example.test/k.zip").headways is False


def _trips(count: int) -> str:
    return "route_id,service_id,trip_id\n" + "".join(f"R1,s,T{i}\n" for i in range(1, count + 1))


def _frequencies(*trip_ids: str, header: str = "trip_id,start_time,end_time,headway_secs") -> str:
    return header + "\n" + "".join(f"{t},05:00:00,23:00:00,300\n" for t in trip_ids)


def _added_headways(tmp_path, *, trips: int, frequencies: str | None) -> bool:
    tables = {**GOOD, "trips.txt": _trips(trips)}
    if frequencies is not None:
        tables["frequencies.txt"] = frequencies
    return feeds.add(gtfs_zip(tmp_path / "feed.zip", tables)).headways


@pytest.mark.parametrize("trips,frequencies,expected", [
    pytest.param(1, _frequencies("T1"), True, id="the one trip is named"),
    pytest.param(1, None, False, id="no frequencies.txt"),
    pytest.param(20, _frequencies("T1", "T2", "T3", "T4"), False,
                 id="four rows among twenty trips"),
    pytest.param(4, _frequencies("T1", "T2"), True, id="exactly half the trips"),
    pytest.param(5, _frequencies("T1", "T2"), False, id="just under half"),
    pytest.param(4, _frequencies("T1", "T1", "T1", "T1"), False,
                 id="four rows for one trip are one trip"),
    pytest.param(2, _frequencies("X1", "X2"), False, id="rows naming trips the feed lacks"),
    pytest.param(1, _frequencies(), False, id="a header and no rows"),
    pytest.param(1, "", False, id="an empty file"),
    pytest.param(1, "start_time,end_time,headway_secs\n05:00:00,23:00:00,300\n", False,
                 id="rows with no trip_id"),
    pytest.param(1, _frequencies("T1", header=" trip_id ,start_time, end_time,headway_secs"),
                 True, id="a padded header, as Metra's is"),
])
def test_a_feed_added_from_a_zip_publishes_headways_when_frequencies_name_half_its_trips(
        home, tmp_path, trips, frequencies, expected):
    assert _added_headways(tmp_path, trips=trips, frequencies=frequencies) is expected


def test_a_user_feeds_headways_are_on_its_record_and_a_restart_does_not_open_the_zip(
        home, tmp_path):
    tables = {**GOOD, "frequencies.txt": _frequencies("T1")}
    feed = feeds.add(gtfs_zip(tmp_path / "with.zip", tables), key="with")
    plain = feeds.add(gtfs_zip(tmp_path / "plain.zip"), key="plain")
    assert (feed.headways, plain.headways) == (True, False)
    assert {r["key"]: r["headways"] for r in json.loads(feeds.user_file().read_text())} == {
        "with": True, "plain": False}
    # With its zip gone, the registry still says it: the record is the answer.
    feed.zip_path.unlink()
    assert feeds.get("with").headways is True
    assert feeds.user_feeds()["with"].headways is True
    assert feeds.get("plain").headways is False


def test_a_record_written_before_the_field_existed_reads_as_no_headways(home, tmp_path):
    old = {k: v for k, v in feeds.Feed(key="old", name="Old", url="", source="user")
           .to_dict().items() if k != "headways"}
    assert "headways" not in old
    feeds.user_file().parent.mkdir(parents=True)
    feeds.user_file().write_text(json.dumps([old]), encoding="utf-8")
    assert feeds.get("old").headways is False
    assert feeds.all()["old"].headways is False
    # The next write states it, for the old record as for the new.
    feeds.add(gtfs_zip(tmp_path / "new.zip"), key="new")
    assert {r["key"]: r["headways"] for r in json.loads(feeds.user_file().read_text())} == {
        "old": False, "new": False}
