"""How large a feed may be, held where it comes in (issue 81).

A feed's download is counted as it streams and stopped at a limit; its zip is
checked from its end record and its headers before anything is read, and
every file in it is read a piece at a time through a count that holds the
same limits, because a header can lie. Read whole, a 1.5 MB zip once made the
normalisation peak at 1.3 GB. The memory a refusal costs is measured with
``tracemalloc``, which sees every bytes object zipfile and zlib make.

A refusal is a ``FeedError`` like any other: over the protocol it is kind
``feed``, its sentence is the hint, and its detail names ``FeedError`` as
every feed error's does. No caller turns one into an empty answer.
"""

import struct
import tracemalloc
import warnings
import zipfile
from contextlib import contextmanager

import pytest

from schematic import config, feeds, loom, pipeline, sample_thumbnails
from test_feeds import GOOD, _frequencies, gtfs_zip, home  # noqa: F401 - home is a fixture
from test_layouts import FakeLoom
from test_serve import DATE, KEY as LA, Client, small_home

MB = 10**6
# A stop_times row, many times over: CSV that deflates about 400 to one.
ROW = b"T1,06:00:00,06:00:00,A,1\n"
PIECE = ROW * ((1 << 20) // len(ROW))


@contextmanager
def peak():
    """The most memory Python allocated inside the block, in ``seen[0]``."""
    seen = [0]
    tracemalloc.start()
    try:
        yield seen
    finally:
        seen[0] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()


def _rows(size: int, piece: bytes):
    while size > 0:
        yield piece[:size]
        size -= len(piece)


def _feed_zip(path, big: dict[str, int], *, method: int = zipfile.ZIP_DEFLATED,
              piece: bytes = PIECE, tables=GOOD):
    """``tables`` written small, then each of ``big`` streamed to its size in
    bytes of ``piece`` over and over, compressed with ``method``."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, body in tables.items():
            if name not in big:
                z.writestr(name, body)
        for name, size in big.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = method
            with z.open(info, "w") as out:
                for chunk in _rows(size, piece):
                    out.write(chunk)
    return path


def _claim(path, member: str, size: int) -> None:
    """Make the zip's headers say ``member`` unpacks to ``size`` bytes, in its
    local header and its central directory, whatever its bytes unpack to."""
    with zipfile.ZipFile(path) as zf:
        info = zf.getinfo(member)
        directory = zf.start_dir
    raw = bytearray(path.read_bytes())
    struct.pack_into("<I", raw, info.header_offset + 22, size)
    at = directory
    while raw[at:at + 4] == b"PK\x01\x02":
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", raw, at + 28)
        if raw[at + 46:at + 46 + name_length].decode() == member:
            struct.pack_into("<I", raw, at + 24, size)
        at += 46 + name_length + extra_length + comment_length
    path.write_bytes(bytes(raw))


def _preset(monkeypatch, path) -> feeds.Feed:
    """A preset ``t`` whose zip is ``path``'s, already in the cache."""
    feed = feeds.Feed(key="t", name="T", url="https://example.test/t.zip")
    monkeypatch.setitem(feeds.FEEDS, "t", feed)
    config.feeds_dir().mkdir(parents=True, exist_ok=True)
    feed.zip_path.write_bytes(path.read_bytes())
    return feed


def _nothing_kept() -> None:
    assert not feeds.user_file().exists()
    folder = config.feeds_dir()
    left = sorted(p.name for p in folder.iterdir()) if folder.exists() else []
    assert left == [], left


# ------------------------------------------------------ from the headers


def test_a_zip_whose_headers_say_one_file_unpacks_past_the_limit_is_refused(home, tmp_path):
    src = gtfs_zip(tmp_path / "bomb.zip")
    _claim(src, "stop_times.txt", feeds.MAX_TABLE_BYTES + 1)
    with pytest.raises(feeds.FeedError) as refused:
        feeds.add(src)
    assert str(refused.value) == ("bomb.zip is too large to read: stop_times.txt unpacks to "
                                  "more than 1 GB, the most one file in a feed may; "
                                  "use a feed filtered to the operator you need")
    _nothing_kept()


def test_a_zip_whose_files_say_they_unpack_past_the_limit_together_is_refused(
        home, tmp_path, monkeypatch):
    """Each under the limit for one file, the three over the limit for all:
    the normalisation, where the 1.3 GB was measured, refuses before a read."""
    src = gtfs_zip(tmp_path / "feed.zip")
    for member in ("stop_times.txt", "trips.txt", "stops.txt"):
        _claim(src, member, feeds.MAX_FEED_BYTES // 3 + 1)
    _preset(monkeypatch, src)
    with pytest.raises(feeds.FeedError) as refused:
        feeds.normalize("t", force=True)
    assert str(refused.value) == ("'t' is too large to read: its files unpack to more than "
                                  "2 GB together, the most a feed may; use a feed filtered "
                                  "to the operator you need")
    assert sorted(p.name for p in config.feeds_dir().iterdir()) == ["t.zip"]


def test_a_small_zip_that_unpacks_past_the_limit_is_refused_without_inflating_it(
        home, tmp_path, monkeypatch):
    monkeypatch.setattr(feeds, "MAX_TABLE_BYTES", 1 * MB)
    src = _feed_zip(tmp_path / "bomb.zip", {"stop_times.txt": 20 * MB})
    assert src.stat().st_size < 100_000, "a bomb: small outside, twenty times the limit inside"
    with peak() as seen, pytest.raises(feeds.FeedError, match="unpacks to more than 1 MB"):
        feeds.add(src)
    assert seen[0] < 1 * MB, seen[0]
    _nothing_kept()


@pytest.mark.parametrize("method", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA], ids=["bzip2", "lzma"])
def test_a_table_compressed_so_it_inflates_in_one_step_is_refused_unread(
        home, tmp_path, monkeypatch, method):
    """zipfile hands a bzip2 or LZMA member to a decompressor with no limit,
    so asking it for 64 kB inflates the whole file at once. Within the limits
    by its headers, and still refused."""
    # Zeros, which bzip2 compresses in a moment and rows of a table it does not.
    src = _feed_zip(tmp_path / "feed.zip", {"stop_times.txt": 32 * MB}, method=method,
                    piece=bytes(1 << 20))
    _preset(monkeypatch, src)
    with peak() as seen, pytest.raises(feeds.FeedError) as refused:
        feeds.normalize("t", force=True)
    assert str(refused.value) == ("'t' compresses stop_times.txt in a way the engine cannot "
                                  "read a piece at a time; re-zip it with the standard "
                                  "Deflate method")
    assert seen[0] < 4 * MB, seen[0]


# ------------------------------------------------------- while reading


def test_a_large_table_is_copied_across_a_piece_at_a_time_and_whole(home, tmp_path, monkeypatch):
    """A normal feed passes the limits, and the normalisation copies a table
    it does not rewrite without ever holding it: 64 MB across, a few MB held."""
    src = _feed_zip(tmp_path / "feed.zip", {"stop_times.txt": 64 * MB})
    _preset(monkeypatch, src)
    with peak() as seen:
        out = feeds.normalize("t", force=True)
    assert seen[0] < 8 * MB, seen[0]
    with zipfile.ZipFile(src) as before, zipfile.ZipFile(out) as after:
        old, new = before.getinfo("stop_times.txt"), after.getinfo("stop_times.txt")
        assert (new.file_size, new.CRC) == (old.file_size, old.CRC) == (64 * MB, old.CRC)
        assert after.testzip() is None


def test_a_header_that_understates_a_file_cannot_get_it_inflated_whole(home, tmp_path, monkeypatch):
    """The headers say 1,000 bytes and the bytes unpack to 64 MB: read a piece
    at a time, zipfile stops at what the header said and the CRC fails, and
    no half-written copy is left beside the zips."""
    src = _feed_zip(tmp_path / "feed.zip", {"stop_times.txt": 64 * MB})
    _claim(src, "stop_times.txt", 1000)
    _preset(monkeypatch, src)
    with peak() as seen, pytest.raises(zipfile.BadZipFile, match="Bad CRC-32"):
        feeds.normalize("t", force=True)
    assert seen[0] < 8 * MB, seen[0]
    assert sorted(p.name for p in config.feeds_dir().iterdir()) == ["t.zip"]


def test_a_name_the_zip_lists_three_times_is_stopped_by_the_count(home, tmp_path, monkeypatch):
    """The headers add up to under the limit for the whole feed, but a name
    listed three times is read three times, the last entry each time: what is
    counted is what is read."""
    monkeypatch.setattr(feeds, "MAX_TABLE_BYTES", 1 * MB)
    monkeypatch.setattr(feeds, "MAX_FEED_BYTES", 2 * MB)
    src = tmp_path / "feed.zip"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)  # zipfile's "Duplicate name"
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            for name, body in GOOD.items():
                if name != "stop_times.txt":
                    z.writestr(name, body)
            z.writestr("stop_times.txt", GOOD["stop_times.txt"])
            z.writestr("stop_times.txt", GOOD["stop_times.txt"])
            z.writestr("stop_times.txt", PIECE[:900_000])
    with zipfile.ZipFile(src) as zf:
        assert sum(i.file_size for i in zf.infolist()) < 1 * MB
    _preset(monkeypatch, src)
    with pytest.raises(feeds.FeedError) as refused:
        feeds.tables("t", normalized=False)
    assert str(refused.value) == ("'t' is too large to read: its files unpack to more than "
                                  "2 MB together, the most a feed may; use a feed filtered "
                                  "to the operator you need")


def test_the_count_refuses_a_file_past_the_limit_whatever_the_headers_said(
        home, tmp_path, monkeypatch):
    """With the headers' check out of the way, as a header that says nothing
    true would leave it, the count alone stops the read at the limit."""
    monkeypatch.setattr(feeds, "MAX_TABLE_BYTES", 1 * MB)
    monkeypatch.setattr(feeds, "_check_sizes", lambda zf, what: None)
    src = _feed_zip(tmp_path / "feed.zip", {"stop_times.txt": 20 * MB})
    _preset(monkeypatch, src)
    with peak() as seen, pytest.raises(feeds.FeedError) as refused:
        feeds.normalize("t", force=True)
    assert str(refused.value) == ("'t' is too large to read: stop_times.txt unpacks to more "
                                  "than 1 MB, the most one file in a feed may; use a feed "
                                  "filtered to the operator you need")
    assert seen[0] < 4 * MB, seen[0]
    assert sorted(p.name for p in config.feeds_dir().iterdir()) == ["t.zip"]


def test_a_normal_feed_is_added_normalised_and_read_within_the_limits(home, tmp_path):
    feed = feeds.add(gtfs_zip(tmp_path / "small.zip"))
    out = feeds.normalize(feed)
    with zipfile.ZipFile(feed.zip_path) as before, zipfile.ZipFile(out) as after:
        for name in ("stops.txt", "trips.txt", "stop_times.txt", "calendar.txt"):
            assert after.read(name) == before.read(name), name
    assert set(feeds.tables(feed)) == {"agency", "stops", "routes", "trips", "stop_times",
                                       "calendar"}


def test_every_preset_as_cached_is_within_the_limits():
    """The limits are generous: every preset's zip on this machine passes
    them. The Chicago 'L', the largest, unpacks to 433 MB (Oct 2026)."""
    cached = [f for f in feeds.FEEDS.values() if f.zip_path.exists()]
    if not cached:
        pytest.skip("needs the presets' zips under data/feeds")
    for feed in cached:
        with zipfile.ZipFile(feed.zip_path) as zf:
            feeds._check_sizes(zf, repr(feed.key))
        assert feed.zip_path.stat().st_size <= feeds.MAX_DOWNLOAD_BYTES, feed.key


# ------------------------------------------------------------ download


def _endless(monkeypatch, *, length: str | None = None, chunks: int = 100):
    """``requests.get`` answering with ``chunks`` pieces of 64 kB, and a list
    counting the pieces it was asked for."""
    pulled = []

    class Response:
        headers = {"Content-Length": length} if length is not None else {}

        def raise_for_status(self):
            pass

        def iter_content(self, size):
            for _ in range(chunks):
                pulled.append(1)
                yield b"\0" * (1 << 16)

    monkeypatch.setattr(feeds.requests, "get", lambda url, **kw: Response())
    return pulled


def test_a_download_past_the_limit_is_stopped_mid_stream_and_keeps_nothing(home, monkeypatch):
    monkeypatch.setattr(feeds, "MAX_DOWNLOAD_BYTES", 200_000)
    pulled = _endless(monkeypatch)
    heard = []
    with pytest.raises(feeds.FeedError) as refused:
        feeds.add("https://example.test/big.zip?key=secret",
                  progress=lambda stage, done, total: heard.append(done))
    assert str(refused.value) == ("https://example.test/big.zip?key=<redacted> could not be "
                                  "fetched: it is larger than 200 kB, the most a feed may be "
                                  "to download; use a feed filtered to the operator you need")
    # Three pieces fit; the fourth passes the limit and is the last asked for.
    assert len(pulled) == 4
    assert heard and max(heard) <= 200_000
    _nothing_kept()


def test_a_download_that_says_it_is_past_the_limit_is_refused_before_a_byte(home, monkeypatch):
    pulled = _endless(monkeypatch, length=str(feeds.MAX_DOWNLOAD_BYTES + 1))
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T",
                                                     url="https://example.test/t.zip"))
    with pytest.raises(feeds.FeedError) as refused:
        feeds.fetch("t")
    assert str(refused.value) == ("https://example.test/t.zip could not be fetched: it is "
                                  "larger than 500 MB, the most a feed may be to download; "
                                  "use a feed filtered to the operator you need")
    assert pulled == []
    assert list(config.feeds_dir().iterdir()) == [], "no zip and no .part"


# ------------------------------------------------ what is not counted


def test_a_large_file_that_is_not_a_table_is_neither_counted_nor_read(home, tmp_path):
    """A readme or a video beside the tables is never read, so its size is
    not judged: one that says it unpacks past the limit for a file, and three
    that say they pass the limit for a feed together, leave the feed added,
    normalised and read."""
    extras = {"readme.pdf": "x", "a.mp4": "x", "b.mp4": "x", "c.mp4": "x"}
    src = gtfs_zip(tmp_path / "with extras.zip", {**GOOD, **extras})
    _claim(src, "readme.pdf", feeds.MAX_TABLE_BYTES + 1)
    for name in ("a.mp4", "b.mp4", "c.mp4"):
        _claim(src, name, feeds.MAX_FEED_BYTES // 3 + 1)
    feed = feeds.add(src)
    with zipfile.ZipFile(feeds.normalize(feed)) as z:
        assert not set(extras) & set(z.namelist())
    assert "stop_times" in feeds.tables(feed, normalized=False)


def test_a_table_the_engine_never_reads_is_not_judged_by_its_compression_until_read(
        home, tmp_path):
    """pathways.txt, translations.txt and attributions.txt never reach LOOM
    and nothing in the engine reads them, so a bzip2 one is no reason to
    refuse a feed. Read all the same, through tables(), it is refused."""
    src = tmp_path / "skip.zip"
    with zipfile.ZipFile(src, "w") as z:
        for name, body in GOOD.items():
            z.writestr(name, body)
        z.writestr("pathways.txt", "pathway_id\np\n", compress_type=zipfile.ZIP_BZIP2)
    feed = feeds.add(src)
    with zipfile.ZipFile(feeds.normalize(feed)) as z:
        assert "pathways.txt" not in z.namelist()
    with pytest.raises(feeds.FeedError) as refused:
        feeds.tables(feed, normalized=False)
    assert str(refused.value) == ("'metro-de-prueba' compresses pathways.txt in a way the "
                                  "engine cannot read a piece at a time; re-zip it with the "
                                  "standard Deflate method")


# ------------------------------------------------------ how many files


def _many(path, entries: int):
    """A feed's tables and empty files beside them, ``entries`` in all."""
    with zipfile.ZipFile(path, "w") as z:
        for name, body in GOOD.items():
            z.writestr(name, body)
        for i in range(entries - len(GOOD)):
            z.writestr(f"x/{i}", b"")
    return path


def _end_record(path) -> int:
    raw = path.read_bytes()
    return struct.unpack_from("<H", raw, raw.rfind(b"PK\x05\x06") + 10)[0]


@pytest.mark.parametrize("entries", [20_000, 70_000], ids=["20,000", "70,000 in ZIP64"])
def test_a_zip_of_too_many_files_is_refused_before_its_directory_is_parsed(
        home, tmp_path, entries):
    """Opening a zip parses one ZipInfo per entry, about 560 bytes each: the
    count in its end record is read first. Past 65,535 entries the count is
    in the ZIP64 end record instead."""
    src = _many(tmp_path / "many.zip", entries)
    assert _end_record(src) == (0xFFFF if entries > 0xFFFF else entries)
    with peak() as seen, pytest.raises(feeds.FeedError) as refused:
        feeds.add(src)
    assert str(refused.value) == ("many.zip holds more than 10,000 files, where a feed has a "
                                  "few dozen; re-zip it with only the feed's tables")
    assert seen[0] < 1 * MB, seen[0]
    _nothing_kept()


def test_a_zip_whose_end_record_understates_its_files_is_refused_once_parsed(home, tmp_path):
    """zipfile reads the directory by its size, never by the count, so an end
    record saying 7 is caught by the count of what was parsed."""
    src = _many(tmp_path / "many.zip", 20_000)
    raw = bytearray(src.read_bytes())
    struct.pack_into("<HH", raw, raw.rfind(b"PK\x05\x06") + 8, 7, 7)
    src.write_bytes(bytes(raw))
    assert feeds._entries(src) == 7
    with pytest.raises(feeds.FeedError, match="^many.zip holds more than 10,000 files"):
        feeds.add(src)
    _nothing_kept()


# ------------------------------------------------- unpacked for LOOM


OVER = ("bomb.normalized.zip is too large to read: {} unpacks to more than 1 MB, the most "
        "one file in a feed may; use a feed filtered to the operator you need")


@pytest.mark.parametrize("member,method,said", [
    pytest.param("stop_times.txt", zipfile.ZIP_DEFLATED, OVER.format("stop_times.txt"),
                 id="a table, by its headers"),
    pytest.param("notes.txt", zipfile.ZIP_DEFLATED, OVER.format("notes.txt"),
                 id="another file, by the count"),
    pytest.param("notes.txt", zipfile.ZIP_BZIP2,
                 "bomb.normalized.zip compresses notes.txt in a way the engine cannot read a "
                 "piece at a time; re-zip it with the standard Deflate method",
                 id="another file, by its compression"),
])
def test_a_zip_unpacked_for_gtfs2graph_is_held_to_the_limits(tmp_path, monkeypatch,
                                                            member, method, said):
    """The native backend hands gtfs2graph the feed unpacked. A table that
    says it is over a limit is refused before a byte is written; a file that
    is not a table is not judged by its header but is counted, and its
    compression checked, as it is written out. Nothing of it is left."""
    monkeypatch.setattr(feeds, "MAX_TABLE_BYTES", 1 * MB)
    zipped = _feed_zip(tmp_path / "bomb.normalized.zip", {member: 20 * MB}, method=method,
                       piece=bytes(1 << 20) if method == zipfile.ZIP_BZIP2 else PIECE)
    with peak() as seen, pytest.raises(feeds.FeedError) as refused:
        loom.unpacked(zipped)
    assert str(refused.value) == said
    assert seen[0] < 4 * MB, seen[0]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bomb.normalized.zip"]


# ----------------------------------------- a refusal reaches the caller
#
# Every request that reads a feed's tables, over the in-process server and
# test_serve's small timetabled LA feed, laid out by the stand-in LOOM. The
# limit for a feed's files together is then lowered under that feed's size,
# and each request is answered with the refusal: kind feed, the sentence as
# the hint, and a detail that names FeedError as every feed error's does.

LIMIT = 200
SAID = (f"{LA!r} is too large to read: its files unpack to more than 200 bytes together, "
        "the most a feed may; use a feed filtered to the operator you need")


@pytest.fixture
def server(tmp_path, monkeypatch):
    small_home(tmp_path, monkeypatch)
    FakeLoom(monkeypatch)
    client = Client()
    yield client
    client.endpoint.close()


def _refused(response: dict, sentence: str) -> None:
    assert "error" in response, response
    error = response["error"]
    assert error["data"]["kind"] == "feed", error
    assert error["message"] == error["data"]["hint"] == sentence
    detail = error["data"]["detail"]
    assert detail.startswith(f"schematic.feeds.FeedError: {sentence} (feeds.py:"), detail


def test_feeds_add_answers_a_limit_as_it_answers_a_missing_table(server, tmp_path, monkeypatch):
    missing = gtfs_zip(tmp_path / "Shelbyville.zip",
                       {k: v for k, v in GOOD.items() if k != "stops.txt"})
    _refused(server.call("feeds.add", {"source": str(missing)}),
             "Shelbyville.zip has no stops.txt, so there is no network to draw")
    monkeypatch.setattr(feeds, "MAX_FEED_BYTES", LIMIT)
    src = gtfs_zip(tmp_path / "Springfield.zip")
    _refused(server.call("feeds.add", {"source": str(src)}),
             "Springfield.zip is too large to read: its files unpack to more than 200 bytes "
             "together, the most a feed may; use a feed filtered to the operator you need")


@pytest.mark.parametrize("method,params", [
    ("feeds.inspect", {"key": LA}),
    ("feeds.service", {"key": LA, "anchor": DATE}),
    ("graph.build", {"key": LA}),
])
def test_a_request_that_reads_the_feed_is_refused_at_the_limit(server, monkeypatch,
                                                                method, params):
    monkeypatch.setattr(feeds, "MAX_FEED_BYTES", LIMIT)
    _refused(server.call(method, params), SAID)
    assert pipeline.stored(LA) is None


@pytest.mark.parametrize("method,params", [
    ("render.stage", {"stage": "octi", "date": DATE}),
    ("map.build", {"date": DATE}),
])
def test_a_request_that_reads_a_layouts_timetable_is_refused_at_the_limit(server, monkeypatch,
                                                                          method, params):
    layout = server.call("graph.build", {"key": LA})["result"]["layout"]
    monkeypatch.setattr(feeds, "MAX_FEED_BYTES", LIMIT)
    _refused(server.call(method, {"key": LA, "layout": layout, **params}), SAID)


def test_what_reads_no_table_answers_whatever_the_feeds_size(server, tmp_path, monkeypatch):
    """render.stage without a date reads no timetable, and the sample
    thumbnails read the stored layout alone: neither opens the feed's zip,
    so neither meets a limit, and neither turns one into no picture."""
    layout = server.call("graph.build", {"key": LA})["result"]["layout"]
    monkeypatch.setattr(feeds, "MAX_FEED_BYTES", LIMIT)
    assert "result" in server.call("render.stage", {"key": LA, "layout": layout,
                                                     "stage": "octi"})
    report = sample_thumbnails.write(tmp_path / "thumbnails")
    assert LA not in report.missing, report.missing
    assert {p.name for p in report.written} >= {f"{LA}-dark.svg", f"{LA}-light.svg"}


@pytest.mark.parametrize("tables,member", [
    pytest.param(GOOD, "agency.txt", id="the agency, read for its name"),
    pytest.param({**GOOD, "frequencies.txt": _frequencies("T1")}, "frequencies.txt",
                 id="the frequencies, read for its headways"),
])
def test_a_limit_met_while_add_reads_a_table_is_a_refusal_not_an_unreadable_table(
        home, tmp_path, monkeypatch, tables, member):
    """add answers no name, or no headways, for a table it cannot read; a
    limit met while it reads one is not that, and is refused. The headers'
    check is out of the way, as a header that says nothing true would leave
    it, so it is the count that meets the limit."""
    monkeypatch.setattr(feeds, "_check_sizes", lambda zf, what: None)
    monkeypatch.setattr(feeds, "MAX_TABLE_BYTES", 60)
    src = gtfs_zip(tmp_path / "Springfield.zip", tables)
    with pytest.raises(feeds.FeedError) as refused:
        feeds.add(src)
    assert str(refused.value) == (f"Springfield.zip is too large to read: {member} unpacks to "
                                  "more than 60 bytes, the most one file in a feed may; use a "
                                  "feed filtered to the operator you need")
    _nothing_kept()
