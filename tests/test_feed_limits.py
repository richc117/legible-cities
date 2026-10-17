"""How large a feed may be, held where it comes in (issue 81).

A feed's download is counted as it streams and stopped at a limit; its zip is
checked from its headers before anything is read, and every file in it is
read a piece at a time through a count that holds the same limits, because a
header can lie. Read whole, a 1.5 MB zip once made the normalisation peak at
1.3 GB. The memory a refusal costs is measured with ``tracemalloc``, which
sees every bytes object zipfile and zlib make.
"""

import struct
import tracemalloc
import warnings
import zipfile
from contextlib import contextmanager

import pytest

from schematic import config, feeds
from test_feeds import GOOD, gtfs_zip, home  # noqa: F401 - home is a fixture

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
    with pytest.raises(feeds.TooLarge) as refused:
        feeds.add(src)
    assert str(refused.value) == ("bomb.zip is too large to read: stop_times.txt unpacks to "
                                  "more than 1 GB, the most one file in a feed may")
    _nothing_kept()


def test_a_zip_whose_files_say_they_unpack_past_the_limit_together_is_refused(
        home, tmp_path, monkeypatch):
    """Each under the limit for one file, the three over the limit for all:
    the normalisation, where the 1.3 GB was measured, refuses before a read."""
    src = gtfs_zip(tmp_path / "feed.zip")
    for member in ("stop_times.txt", "trips.txt", "stops.txt"):
        _claim(src, member, feeds.MAX_FEED_BYTES // 3 + 1)
    _preset(monkeypatch, src)
    with pytest.raises(feeds.TooLarge) as refused:
        feeds.normalize("t", force=True)
    assert str(refused.value) == ("'t' is too large to read: its files unpack to more than "
                                  "2 GB together, the most a feed may")
    assert sorted(p.name for p in config.feeds_dir().iterdir()) == ["t.zip"]


def test_a_small_zip_that_unpacks_past_the_limit_is_refused_without_inflating_it(
        home, tmp_path, monkeypatch):
    monkeypatch.setattr(feeds, "MAX_TABLE_BYTES", 1 * MB)
    src = _feed_zip(tmp_path / "bomb.zip", {"stop_times.txt": 20 * MB})
    assert src.stat().st_size < 100_000, "a bomb: small outside, twenty times the limit inside"
    with peak() as seen, pytest.raises(feeds.TooLarge, match="unpacks to more than 1 MB"):
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
    assert str(refused.value) == ("'t' compresses stop_times.txt in a way the engine does "
                                  "not read; a feed's tables are read stored or deflated")
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
    with pytest.raises(feeds.TooLarge) as refused:
        feeds.tables("t", normalized=False)
    assert str(refused.value) == ("'t' is too large to read: its files unpack to more than "
                                  "2 MB together, the most a feed may")


def test_the_count_refuses_a_file_past_the_limit_whatever_the_headers_said(
        home, tmp_path, monkeypatch):
    """With the headers' check out of the way, as a header that says nothing
    true would leave it, the count alone stops the read at the limit."""
    monkeypatch.setattr(feeds, "MAX_TABLE_BYTES", 1 * MB)
    monkeypatch.setattr(feeds, "_check_sizes", lambda zf, what: None)
    src = _feed_zip(tmp_path / "feed.zip", {"stop_times.txt": 20 * MB})
    _preset(monkeypatch, src)
    with peak() as seen, pytest.raises(feeds.TooLarge) as refused:
        feeds.normalize("t", force=True)
    assert str(refused.value) == ("'t' is too large to read: stop_times.txt unpacks to more "
                                  "than 1 MB, the most one file in a feed may")
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
    with pytest.raises(feeds.TooLarge) as refused:
        feeds.add("https://example.test/big.zip?key=secret",
                  progress=lambda stage, done, total: heard.append(done))
    assert str(refused.value) == ("https://example.test/big.zip?key=<redacted> could not be "
                                  "fetched: it is larger than 200 kB, the most a feed may be "
                                  "to download")
    # Three pieces fit; the fourth passes the limit and is the last asked for.
    assert len(pulled) == 4
    assert heard and max(heard) <= 200_000
    _nothing_kept()


def test_a_download_that_says_it_is_past_the_limit_is_refused_before_a_byte(home, monkeypatch):
    pulled = _endless(monkeypatch, length=str(feeds.MAX_DOWNLOAD_BYTES + 1))
    monkeypatch.setitem(feeds.FEEDS, "t", feeds.Feed(key="t", name="T",
                                                     url="https://example.test/t.zip"))
    with pytest.raises(feeds.TooLarge) as refused:
        feeds.fetch("t")
    assert str(refused.value) == ("https://example.test/t.zip could not be fetched: it is "
                                  "larger than 500 MB, the most a feed may be to download")
    assert pulled == []
    assert list(config.feeds_dir().iterdir()) == [], "no zip and no .part"
