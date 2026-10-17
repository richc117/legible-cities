"""GTFS feed registry and local cache.

Feeds are downloaded once into ``data/feeds`` under the engine's home (see
``config``) and reused. The registry has two halves: the presets in ``FEEDS``,
curated here in Python, and the feeds a person added, kept as JSON beside the
zips (``user-feeds.json`` under the home) so they survive a restart. ``all()``
is both, ``get(key)`` looks in both, ``add()`` and ``remove()`` change the
second half only. Nothing downstream in the pipeline is city-specific.
"""

from __future__ import annotations

import builtins
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import struct
import threading
import time
import zipfile
from contextlib import contextmanager
from http import HTTPStatus
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import IO, Any, Callable, Iterator
from urllib.parse import urlsplit

import pandas as pd
import requests

from . import config

# How large a feed may be: its download, what any one of its tables unpacks
# to, what its tables unpack to together, and how many files its zip holds.
# Generous on purpose -- the largest preset, the Chicago 'L', is a 100 MB
# download that unpacks to 433 MB, 373 MB of it stop_times.txt, and the most
# files any preset's zip holds is 32 (Oct 2026) -- and there so that a zip far
# larger inside than out is refused with a sentence instead of taking the
# engine's memory: read whole, a 1.5 MB zip once made the normalisation peak
# at 1.3 GB (issue 81). A download is counted as it streams and stopped at its
# limit. The sizes are checked from the zip's headers before anything is
# read, and counted again as each file is read a piece at a time, because a
# header can lie.
#
# They bound the bytes read out of the zip and nothing else. A table within
# them is still parsed whole into a DataFrame, and pandas holds several times
# a table's size while it does: a 1 GB stop_times.txt is several GB resident,
# and Chicago's 373 MB one already costs about 2.5 GB.
MAX_DOWNLOAD_BYTES = 500_000_000
MAX_TABLE_BYTES = 1_000_000_000
MAX_FEED_BYTES = 2_000_000_000
MAX_ENTRIES = 10_000
# The most a zip's central directory may say it is: 46 bytes an entry before
# its name and extras, for every entry allowed, and room for long names. The
# count alone is not enough, because zipfile parses the directory by its size
# and never by the count, at about 560 bytes of memory for every entry it
# finds; a preset's directory is a few kB.
MAX_DIRECTORY_BYTES = MAX_ENTRIES * 46 + 10_000_000

# A GTFS zip is only *mostly* GTFS. Agencies drop license agreements, readmes
# and spreadsheets in alongside the tables, and reading every .txt as CSV then
# fails on prose (SFMTA ships a license agreement that pandas chokes on).
GTFS_TABLES = frozenset({
    "agency", "stops", "routes", "trips", "stop_times", "calendar",
    "calendar_dates", "fare_attributes", "fare_rules", "shapes", "frequencies",
    "transfers", "pathways", "levels", "feed_info", "translations",
    "attributions", "areas", "stop_areas", "networks", "route_networks",
    "fare_products", "fare_leg_rules", "fare_transfer_rules", "fare_media",
    "timeframes", "booking_rules", "location_groups", "location_group_stops",
})

# Tables a line graph never needs, and which LOOM's strict parser rejects on
# real feeds -- MBTA's pathways.txt uses a negative stair_count, which the spec
# allows for descending stairs but LOOM reads as a non-negative integer.
#
# levels.txt deliberately stays: stops.txt references it by level_id, and
# dropping it turns one parse error into a dangling-reference error.
LOOM_SKIP = frozenset({"pathways", "translations", "attributions"})

@dataclass(frozen=True)
class Feed:
    """A GTFS feed we know how to fetch."""

    key: str
    name: str
    url: str
    # Where it runs, and what the network is called there. `name` cannot serve
    # both: it is inconsistent about whether the city is in it ("BART" and
    # "Metra" have none, "LA Metro Rail" and "Mexico City Metro" already do), so
    # composing city + name mechanically yields "Chicago - Chicago 'L'". These
    # two are what an exported title is built from; `name` stays as the atlas
    # label. Defaulted so the registry stays valid while they are filled in.
    city: str = ""
    network: str = ""
    # LOOM's -m flag: tram, bus, coach, rail, subway, ferry, funicular, gondola, all
    mode: str = "all"
    # Applied to route_long_name when route_short_name is blank; group 1 becomes
    # the line label drawn on the map. See ``route_labels`` for why this matters.
    label_pattern: str | None = None
    # Removed from the label unconditionally. Some agencies publish one route per
    # direction (BART's "Yellow-N" / "Yellow-S"), which would otherwise draw
    # every line twice, side by side, as two separate colours of the same hue.
    label_strip: str | None = None
    # Keep only this agency_id. A city-wide feed can carry several operators,
    # and route_type alone cannot separate them: Mexico City's feed has both
    # Metro Linea 1 and a Ferrocarriles Suburbanos line numbered "1", both
    # route_type 1, which merge into a single line without this.
    agency: str | None = None
    # Facts about the feed that the pipeline cannot work out for itself, shown
    # beside the computed caveats on the atlas.
    # Carry the pre-octilinear geometry into the animation page, so it can show
    # the geographic map straightening into the schematic one. On by default:
    # Geographic is the switcher's first button, and a network without this
    # would show a dead control. It is a second copy of every track, so a page
    # grows about 8% for it. Set False to opt one network out -- the exporter
    # still refuses a geographic export of such a feed rather than quietly
    # rendering the schematic map, which is what `export.check_geographic` is
    # for.
    geographic: bool = True
    notes: tuple[str, ...] = ()
    # Whether the operator publishes headways (``frequencies.txt``: trains at a
    # scheduled interval for each period of the day) rather than a timetable
    # of trip times. A fact about the feed that a client cannot read from the
    # zip without opening it, so the registry states it: set by hand on a
    # preset, decided from the zip for a feed a person adds
    # (``_publishes_headways``) and kept on its record.
    headways: bool = False
    # "preset" for an entry of FEEDS, "user" for one a person added, which is
    # the one kind ``remove`` will take away.
    source: str = "preset"

    @property
    def zip_path(self) -> Path:
        return config.feeds_dir() / f"{self.key}.zip"

    @property
    def shown_url(self) -> str:
        """The address as text that leaves the engine may carry it: a preset's
        as the registry has it, which is public and is the credit the feed is
        owed, and a person's own without its secrets (``shown``)."""
        return self.url if self.source == "preset" else shown(self.url)

    def to_dict(self) -> dict[str, Any]:
        """The record as JSON holds it: every field, notes as a list."""
        d = asdict(self)
        d["notes"] = list(self.notes)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Feed":
        """A record back from JSON. A missing optional field takes its default;
        an unknown one is ignored, so a record from a newer engine still reads."""
        known = {f.name for f in fields(cls)}
        given = {k: v for k, v in d.items() if k in known}
        for name in ("key", "name", "url"):
            if not isinstance(given.get(name), str):
                raise FeedError(f"a feed record needs a {name}")
        if "notes" in given:
            given["notes"] = tuple(str(n) for n in given["notes"])
        return cls(**given)

    @property
    def normalized_zip_path(self) -> Path:
        return config.feeds_dir() / f"{self.key}.normalized.zip"


FEEDS: dict[str, Feed] = {
    "la-metro-rail": Feed(
        key="la-metro-rail",
        name="LA Metro Rail",
        city="Los Angeles",
        network="Metro Rail",
        url="https://gitlab.com/LACMTA/gtfs_rail/-/raw/master/gtfs_rail.zip",
        mode="all",
        # LA leaves route_short_name blank and names routes "Metro A Line".
        label_pattern=r"^Metro\s+(\S+)\s+Line$",
    ),
    "bart": Feed(
        key="bart",
        name="BART",
        city="San Francisco Bay Area",
        network="BART",
        url="https://www.bart.gov/dev/schedules/google_transit.zip",
        mode="all",
        # BART splits each line by direction: Yellow-N, Yellow-S, ...
        label_strip=r"-[NSEW]$",
    ),
    "trimet-max": Feed(
        key="trimet-max",
        name="TriMet MAX",
        city="Portland",
        network="MAX Light Rail",
        url="https://developer.trimet.org/schedule/gtfs.zip",
        # TriMet publishes bus and rail in one feed; MAX is light rail.
        mode="tram",
    ),

    # --- heavy rail / metro --------------------------------------------------
    "nyc-subway": Feed(
        key="nyc-subway",
        name="New York City Subway",
        city="New York",
        network="Subway",
        # web.mta.info answers with a redirect to the MTA's own S3 bucket, and
        # on https it redirects back down to http first, so the bucket's https
        # address is the one that is encrypted end to end. Same bytes as the
        # old address's (checked Oct 2026).
        url="https://rrgtfsfeeds.s3.amazonaws.com/gtfs_subway.zip",
        mode="all",
    ),
    "chicago-l": Feed(
        key="chicago-l",
        name="Chicago 'L'",
        city="Chicago",
        network="The 'L'",
        url="https://www.transitchicago.com/downloads/sch_data/google_transit.zip",
        mode="subway",
    ),
    "boston-t": Feed(
        key="boston-t",
        name="MBTA Subway",
        city="Boston",
        network="MBTA Subway",
        url="https://cdn.mbta.com/MBTA_GTFS.zip",
        # Red/Orange/Blue are heavy rail; the Green Line and Mattapan are light rail.
        mode="tram,subway",
    ),
    "marta": Feed(
        key="marta",
        name="MARTA Rail",
        city="Atlanta",
        network="MARTA Rail",
        url="https://www.itsmarta.com/google_transit_feed/google_transit.zip",
        mode="subway",
    ),
    "miami-metrorail": Feed(
        key="miami-metrorail",
        name="Miami Metrorail & Metromover",
        city="Miami",
        network="Metrorail & Metromover",
        url="https://www.miamidade.gov/transit/googletransit/current/google_transit.zip",
        # Metrorail is published as route_type 2 (rail), not subway; Metromover
        # and the airport people mover are light rail.
        mode="tram,rail",
    ),
    "cleveland-rta": Feed(
        key="cleveland-rta",
        name="Cleveland RTA Rapid",
        city="Cleveland",
        network="RTA Rapid Transit",
        url="https://www.riderta.com/sites/default/files/gtfs/latest/google_transit.zip",
        mode="tram,subway",
    ),

    # --- light rail ----------------------------------------------------------
    "sf-muni-metro": Feed(
        key="sf-muni-metro",
        name="Muni Metro",
        city="San Francisco",
        network="Muni Metro",
        url="https://muni-gtfs.apps.sfmta.com/data/muni_gtfs-current.zip",
        mode="tram",
    ),
    "denver-rtd": Feed(
        key="denver-rtd",
        name="RTD Denver Rail",
        city="Denver",
        network="RTD Rail",
        url="https://www.rtd-denver.com/files/gtfs/google_transit.zip",
        mode="tram,rail",
    ),
    "seattle-link": Feed(
        key="seattle-link",
        name="Sound Transit Link & Sounder",
        city="Seattle",
        network="Link & Sounder",
        url="https://gtfs.sound.obaweb.org/prod/40_gtfs.zip",
        mode="tram,rail",
    ),
    "dallas-dart": Feed(
        key="dallas-dart",
        name="DART Light Rail",
        city="Dallas",
        network="DART Light Rail",
        url="https://www.dart.org/transitdata/latest/google_transit.zip",
        mode="tram",
    ),
    "minneapolis-metro": Feed(
        key="minneapolis-metro",
        name="Metro Transit Light Rail",
        city="Minneapolis",
        network="Metro Light Rail",
        url="https://svc.metrotransit.org/mtgtfs/gtfs.zip",
        mode="tram",
    ),
    "phoenix-valley-metro": Feed(
        key="phoenix-valley-metro",
        name="Valley Metro Rail",
        city="Phoenix",
        network="Valley Metro Rail",
        url="https://phoenixopendata.com/dataset/3eae9a4a-98b9-40c8-8df7-8c00c1756235/"
            "resource/28ccc0a5-49c8-495c-b91f-193de5ce2cb7/download/googletransit.zip",
        mode="tram",
    ),
    "salt-lake-uta": Feed(
        key="salt-lake-uta",
        name="UTA TRAX & FrontRunner",
        city="Salt Lake City",
        network="TRAX & FrontRunner",
        url="https://gtfsfeed.rideuta.com/GTFS.zip",
        mode="tram,rail",
    ),
    "pittsburgh-t": Feed(
        key="pittsburgh-t",
        name="Pittsburgh Light Rail",
        city="Pittsburgh",
        network="The T",
        url="https://www.portauthority.org/developerresources/GTFS.zip",
        # The T is published as route_type 2 (rail); route_type 7 is the
        # Duquesne and Monongahela inclines, which are their own funiculars.
        mode="rail,funicular",
    ),

    # --- commuter rail -------------------------------------------------------
    "metra": Feed(
        key="metra",
        name="Metra",
        city="Chicago",
        network="Metra",
        url="https://schedules.metrarail.com/gtfs/schedule.zip",
        mode="all",
    ),
    "septa-regional-rail": Feed(
        key="septa-regional-rail",
        name="SEPTA Regional Rail",
        city="Philadelphia",
        network="SEPTA Regional Rail",
        url="https://www3.septa.org/developer/google_rail.zip",
        mode="all",
    ),
    "nj-transit-rail": Feed(
        key="nj-transit-rail",
        name="NJ Transit Rail",
        city="New Jersey",
        network="NJ Transit Rail",
        url="https://www.njtransit.com/rail_data.zip",
        mode="all",
    ),
    "lirr": Feed(
        key="lirr",
        name="Long Island Rail Road",
        city="New York",
        network="Long Island Rail Road",
        # The MTA's own S3 bucket, where web.mta.info redirects (checked Oct
        # 2026); the same bytes over https as over the old http address.
        url="https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip",
        mode="all",
    ),

    # --- outside the US ------------------------------------------------------
    "cdmx-metro": Feed(
        key="cdmx-metro",
        name="Mexico City Metro",
        city="Mexico City",
        network="Metro",
        # The city's own open-data host does not respond and its S3 mirror
        # 403s; this is MobilityData's copy of the same SEMOVI feed.
        url="https://storage.googleapis.com/storage/v1/b/mdb-latest/o/"
            "mx-unknown-pumabus-gtfs-1830.zip?alt=media",
        mode="subway",
        # Eight operators share this feed, and Suburbano also has a route
        # numbered 1 at route_type 1.
        agency="METRO",
        # 1,583 frequencies.txt rows cover all 1,204 of its trips (checked Oct
        # 2026). The other presets that carry the file do not run from it:
        # Valley Metro has 4 rows among 18,157 trips, and the Chicago 'L' and
        # the Pittsburgh T ship it empty.
        headways=True,
        notes=(
            "This is a 2025 snapshot: the published feed's service period ran "
            "to December 2025, so the date above is from its own calendar "
            "rather than this week.",
            "The operator publishes headways rather than timetabled times, so "
            "the trains here run at the scheduled interval for each period of "
            "the day, evenly spaced. It shows: on the time chart this network "
            "is a solid band rather than the peaks and troughs the timetabled "
            "cities draw, because the published interval barely varies.",
            "The station icons the Metro is known for are not in the data, and "
            "are the thing this pipeline cannot generate.",
        ),
    ),
}

# Not registered: WMATA (Washington DC Metro) publishes GTFS only behind an API
# key, at https://api.wmata.com/gtfs/rail-gtfs-static.zip. Add a Feed for it once
# ``fetch`` learns to send a key header.


class FeedError(ValueError):
    """Something about a feed a person can act on; str() is the sentence."""


def _size(n: int) -> str:
    """A byte count as a sentence says it: 2 GB, 500 MB, 64 kB."""
    for unit, scale in (("GB", 10**9), ("MB", 10**6), ("kB", 10**3)):
        if n >= scale:
            return f"{n / scale:.3g} {unit}"
    return f"{n} bytes"


# What a person can do about a feed over a limit: an agency's whole-region
# feed is the usual cause, and most publish one per operator or mode as well.
NARROWER = "use a feed filtered to the operator you need"


def _too_large(what: str, member: str) -> FeedError:
    return FeedError(f"{what} is too large to read: {Path(member).name} unpacks to more "
                     f"than {_size(MAX_TABLE_BYTES)}, the most one file in a feed may; "
                     f"{NARROWER}")


def _too_large_together(what: str) -> FeedError:
    return FeedError(f"{what} is too large to read: its files unpack to more than "
                     f"{_size(MAX_FEED_BYTES)} together, the most a feed may; {NARROWER}")


def _too_many(what: str) -> FeedError:
    return FeedError(f"{what} holds more than {MAX_ENTRIES:,} files, where a feed has a few "
                     "dozen; re-zip it with only the feed's tables")


def _too_long(what: str) -> FeedError:
    return FeedError(f"{what} lists its files in more than {_size(MAX_DIRECTORY_BYTES)}, "
                     "where a feed's list is a few kB; re-zip it with only the feed's tables")


def _unreadable(what: str, member: str) -> FeedError:
    return FeedError(f"{what} compresses {Path(member).name} in a way the engine cannot read "
                     "a piece at a time; re-zip it with the standard Deflate method")


def _is_table(name: str) -> bool:
    return name.endswith(".txt") and Path(name).stem in GTFS_TABLES


# How a member of a feed's zip may be compressed to be read. zipfile inflates
# a deflated member a bounded piece at a time, but hands a bzip2 or LZMA one
# to a decompressor with no limit, so a few hundred bytes of either can unpack
# to gigabytes in one call, before anything here could count them. Every
# member of every preset is deflated but one, which is stored (Oct 2026).
READABLE = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


def _end_record(path: Path) -> tuple[int, int] | None:
    """How many entries the zip's end records say it holds, and how many
    bytes its central directory takes, read where zipfile reads them; None
    where there is no end record (zipfile then refuses the zip in its words).

    Read before ``ZipFile`` opens the zip, because opening it parses the whole
    directory into one ZipInfo per entry, about 560 bytes each: a zip of
    empty entries is under 80 bytes an entry on disk, so a 500 MB one would
    make several GB of them before a count could be looked at. Where a ZIP64
    locator sits just before the classic record, zipfile takes the numbers of
    the ZIP64 end record that sits just before the locator, and so does this;
    neither ever seeks to the offset the locator gives, which can be anything
    up to 2**64 - 1."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        tail_size = min(size, 22 + 0xFFFF)
        fh.seek(size - tail_size)
        tail = fh.read(tail_size)
        at = tail.rfind(b"PK\x05\x06")
        if at < 0 or tail_size - at < 22:
            return None
        count, directory = struct.unpack_from("<HI", tail, at + 10)
        if at < 20 or tail[at - 20:at - 16] != b"PK\x06\x07":
            return count, directory
        start = size - tail_size + at - 20 - 56
        if start < 0:
            return count, directory
        try:
            fh.seek(start)
            record = fh.read(56)
        except (OverflowError, OSError, ValueError):
            return None
    if len(record) == 56 and record[:4] == b"PK\x06\x06":
        entries, length = struct.unpack_from("<QQ", record, 32)
        return entries, length
    return count, directory


def _check_sizes(zf: zipfile.ZipFile, what: str) -> None:
    """Refuse a zip whose headers say its tables unpack past the limits, or
    which compresses a table the engine reads in a way that cannot be read a
    piece at a time. Reads the central directory alone; ``_Counted`` holds
    the same limits on the bytes themselves, and ``_reader`` the compression.

    Only the tables count: a readme, a licence or a spreadsheet beside them
    is never read, so its size is nobody's business here. The tables LOOM
    never sees (``LOOM_SKIP``) are not read by the engine either, so their
    compression is not judged unless something does read one."""
    total = 0
    for info in zf.infolist():
        if not _is_table(info.filename):
            continue
        if Path(info.filename).stem not in LOOM_SKIP and info.compress_type not in READABLE:
            raise _unreadable(what, info.filename)
        if info.file_size > MAX_TABLE_BYTES:
            raise _too_large(what, info.filename)
        total += info.file_size
    if total > MAX_FEED_BYTES:
        raise _too_large_together(what)


@contextmanager
def _opened(path: Path, what: str,
            kind: type[zipfile.ZipFile] = zipfile.ZipFile) -> Iterator[zipfile.ZipFile]:
    """``path``'s zip, opened once its end records say it holds no more than
    ``MAX_ENTRIES`` files in a directory of no more than
    ``MAX_DIRECTORY_BYTES``, and yielded once the directory it parsed agrees
    and its headers are within the limits. A zip that zipfile cannot open is
    refused in the sentence a file that is no zip is."""
    record = _end_record(path)
    if record is not None:
        count, directory = record
        if count > MAX_ENTRIES:
            raise _too_many(what)
        if directory > MAX_DIRECTORY_BYTES:
            raise _too_long(what)
    try:
        zf = kind(path)
    except zipfile.BadZipFile as exc:
        raise FeedError(f"{what} is not a zip file, so it is not a GTFS feed") from exc
    with zf:
        if len(zf.filelist) > MAX_ENTRIES:
            raise _too_many(what)
        _check_sizes(zf, what)
        yield zf


# How much of a table is read at a time: the most one step of inflating it
# makes, whatever its header says.
_PIECE = 1 << 16


class _Budget:
    """What one pass over a feed's zip may still unpack, across its files."""

    def __init__(self, what: str) -> None:
        self.what = what
        self.left = MAX_FEED_BYTES


class _Counted(io.RawIOBase):
    """One member of a feed's zip, read a piece at a time and counted, so the
    byte past a limit is a refusal whatever the zip's headers said: a header
    that understates a file, or a name the zip lists twice, cannot get more
    read than the limits allow. Closing it closes the member."""

    def __init__(self, raw: IO[bytes], name: str, budget: _Budget) -> None:
        self._raw = raw
        self._name = name
        self._budget = budget
        self._count = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Any) -> int:
        data = self._raw.read(min(len(buffer), _PIECE))
        self._count += len(data)
        self._budget.left -= len(data)
        if self._count > MAX_TABLE_BYTES:
            raise _too_large(self._budget.what, self._name)
        if self._budget.left < 0:
            raise _too_large_together(self._budget.what)
        buffer[:len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self._raw.close()
        super().close()


def _reader(zf: zipfile.ZipFile, name: str | zipfile.ZipInfo, budget: _Budget, *,
            opened: Callable[[zipfile.ZipInfo], IO[bytes]] | None = None) -> IO[bytes]:
    """``name`` in ``zf``, open for reading through the count, or a refusal
    if it is compressed in a way that cannot be read a piece at a time.
    ``opened`` opens the member when ``zf.open`` would not do: _CountedZip's
    own open is this."""
    info = name if isinstance(name, zipfile.ZipInfo) else zf.getinfo(name)
    if info.compress_type not in READABLE:
        raise _unreadable(budget.what, info.filename)
    raw = opened(info) if opened is not None else zf.open(info)
    return io.BufferedReader(_Counted(raw, info.filename, budget), _PIECE)


@contextmanager
def _member(zf: zipfile.ZipFile, name: str, budget: _Budget) -> Iterator[IO[bytes]]:
    """``name`` in ``zf``, open for reading through the count."""
    with _reader(zf, name, budget) as fh:
        yield fh


class _CountedZip(zipfile.ZipFile):
    """A zip whose every member is read through the count and the check on
    its compression, ``extractall``'s reads included: it opens each member
    it writes out with ``open``, and copies it a piece at a time."""

    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.budget = _Budget(path.name)

    def open(self, name, mode="r", pwd=None, *, force_zip64=False):  # type: ignore[override]
        if mode != "r" or pwd is not None:
            raise ValueError("a feed's zip is opened to be read, with no password")
        return _reader(self, name, self.budget, opened=super().open)


def unpack(path: Path, folder: Path) -> None:
    """Every file in the zip at ``path`` written into ``folder``, as
    ``ZipFile.extractall`` writes them, within the limits: the entry count
    and the tables' headers checked first, and each file read through the
    count, so a zip that unpacks past them is refused with nothing larger
    than a limit written. A refusal names the zip by its file name."""
    with _opened(path, path.name, kind=_CountedZip) as zf:
        zf.extractall(folder)


# --------------------------------------------------------------------------
# The user half of the registry
# --------------------------------------------------------------------------
#
# Feeds a person added, as one JSON file beside the zips under the home. Read
# on every call rather than cached: the file is small, another process (the
# app's, a notebook's) may have written it, and a stale copy is the kind of
# bug nobody finds until a feed added yesterday is missing today.

USER_FILE = "user-feeds.json"
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
# What a feed must contain to be laid out and animated: the graph side needs
# stops, routes and trips; the schedule side needs stop_times and a calendar
# in one of its two forms. Named in the sentence a person reads.
REQUIRED_TABLES = ("stops", "routes", "trips", "stop_times")
CALENDAR_TABLES = ("calendar", "calendar_dates")
_user_lock = threading.Lock()


def user_file() -> Path:
    return config.feeds_dir() / USER_FILE


def user_feeds() -> dict[str, Feed]:
    """The feeds a person added, from disk; empty when there are none."""
    path = user_file()
    if not path.exists():
        return {}
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FeedError(f"{USER_FILE} could not be read: {exc}") from exc
    if not isinstance(records, list):
        raise FeedError(f"{USER_FILE} is not a list of feeds")
    out: dict[str, Feed] = {}
    for record in records:
        feed = Feed.from_dict({**record, "source": "user"})
        out[feed.key] = feed
    return out


def _write_user_feeds(records: dict[str, Feed]) -> None:
    """The whole file, written beside itself and moved into place: a write
    cut short, or a move refused, leaves the registry as it was and no
    ``.part`` beside it, which nothing else would ever remove. The scratch
    name is the writer's own, process and thread, so that another process
    writing this file (a notebook's may) never has its half-written bytes
    moved into place by this one."""
    path = user_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps([f.to_dict() for f in records.values()], indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def all() -> dict[str, Feed]:  # noqa: A001 - the registry's own name for both halves
    """Every feed: the presets, then the ones a person added."""
    return {**FEEDS, **user_feeds()}


def get(key: str) -> Feed:
    """The feed for ``key``, from either half, or a FeedError naming it."""
    feed = FEEDS.get(key)
    if feed is None:
        feed = user_feeds().get(key)
    if feed is None:
        raise FeedError(f"{key!r} is not a registered feed")
    return feed


def slug(name: str) -> str:
    """A key from a name: lower-case letters, digits and hyphens, or "feed"."""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:64].strip("-")
    return s or "feed"


def _check_gtfs(path: Path, what: str) -> dict[str, str]:
    """The tables a feed must carry, or a FeedError saying which is missing.
    Returns the member name per table stem, so the caller can read agency.txt."""
    if not zipfile.is_zipfile(path):
        raise FeedError(f"{what} is not a zip file, so it is not a GTFS feed")
    with _opened(path, what) as zf:
        members = {Path(n).stem: n for n in zf.namelist()
                   if n.endswith(".txt") and not n.endswith("/")}
    for stem in REQUIRED_TABLES:
        if stem not in members:
            why = ("so there is no timetable to animate" if stem == "stop_times"
                   else "so there is no network to draw")
            raise FeedError(f"{what} has no {stem}.txt, {why}")
    if not any(stem in members for stem in CALENDAR_TABLES):
        raise FeedError(f"{what} has neither calendar.txt nor calendar_dates.txt, "
                        "so there is no service day to draw")
    return members


# What reading one member of a zip can raise besides a bad table. The check
# lists the zip's central directory alone, so a member whose bytes fail their
# CRC (BadZipFile), that is encrypted (RuntimeError) or that uses a
# compression the library lacks (NotImplementedError) passes it and fails here.
UNREADABLE = (ValueError, OSError, KeyError,
              zipfile.BadZipFile, RuntimeError, NotImplementedError)


def _agency_name(path: Path, members: dict[str, str], what: str) -> str | None:
    """The first agency's name, when the feed says one."""
    member = members.get("agency")
    if member is None:
        return None
    try:
        with _opened(path, what) as zf, _member(zf, member, _Budget(what)) as fh:
            df = pd.read_csv(fh, dtype=str, skipinitialspace=True)
    except FeedError:
        # A limit, refused in a sentence of its own: never taken for a table
        # that cannot be read, which answers no name.
        raise
    except UNREADABLE:
        return None
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    if "agency_name" not in df.columns or df.empty:
        return None
    value = df["agency_name"].iloc[0]
    return value.strip() if isinstance(value, str) and value.strip() else None


def _trip_ids(zf: zipfile.ZipFile, member: str, budget: _Budget) -> set[str]:
    """The distinct ``trip_id`` values of one table in the zip; none when the
    table has no such column or no rows."""
    with _member(zf, member, budget) as fh:
        df = pd.read_csv(fh, dtype=str, skipinitialspace=True, keep_default_na=False,
                         usecols=lambda column: column.strip().lstrip("\ufeff") == "trip_id")
    df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
    return set(df["trip_id"]) if "trip_id" in df.columns else set()


def _publishes_headways(path: Path, members: dict[str, str], what: str) -> bool:
    """Whether the feed's service is run from ``frequencies.txt``: the file is
    there, has rows, and the trips those rows name are at least half of the
    trips ``trips.txt`` lists.

    The file's presence alone says too little. Valley Metro's feed carries 4
    rows among 18,157 trips, and the Chicago 'L' and the Pittsburgh T ship the
    file with a header only: a timetable all the same. A table that cannot be
    read is not a claim of headways, so it answers no.
    """
    member = members.get("frequencies")
    if member is None:
        return False
    budget = _Budget(what)
    try:
        with _opened(path, what) as zf:
            named = _trip_ids(zf, member, budget)
            if not named:
                return False
            trips = _trip_ids(zf, members["trips"], budget)
    except FeedError:
        # A limit, never taken for a table that cannot be read (issue 81).
        raise
    except UNREADABLE:
        return False
    return bool(trips) and 2 * len(named & trips) >= len(trips)


def _is_url(source: str) -> bool:
    return source.startswith(("http://", "https://"))


# What a secret in an address is replaced with. The desktop app's own
# redaction uses the same marker, so a line redacted here is unchanged by it.
REDACTED = "<redacted>"


def _shown_query(query: str) -> str:
    """A query without its ``?``: each value replaced, and a part with no name,
    or with only ``=`` padding after its first ``=``, replaced whole."""
    parts = re.split(r"([&;])", query)
    for i in range(0, len(parts), 2):
        part = parts[i]
        if not part:
            continue
        equals = part.find("=")
        if equals < 0 or re.fullmatch(r"=+", part[equals + 1:]):
            parts[i] = REDACTED
        elif equals < len(part) - 1:
            parts[i] = part[:equals + 1] + REDACTED
    return "".join(parts)


def shown(url: str) -> str:
    """``url`` as a message, a log line or a file beside an export may carry it.

    A feed a person added can come from a keyed link (``...?api_key=...``), and
    a message is something they may later paste into a bug report. What is kept
    is what says where the feed came from: the scheme, the host and its port,
    the path, and the names of the query's parameters. The user information,
    every query value and the fragment become a marker. A token carried as a
    path segment is not covered: nothing says which segment is the secret.

    The whole address stays on the record and is what ``fetch`` asks for;
    this is only for text that leaves the engine.
    """
    match = re.match(r"[A-Za-z][A-Za-z0-9+.-]*://", url)
    scheme = match.group(0) if match else ""
    rest = url[len(scheme):]
    fragment = ""
    at = rest.find("#")
    if at >= 0:
        fragment = "#" if at == len(rest) - 1 else f"#{REDACTED}"
        rest = rest[:at]
    query = ""
    at = rest.find("?")
    if at >= 0:
        query = "?" + _shown_query(rest[at + 1:])
        rest = rest[:at]
    at = rest.find("/")
    authority, path = (rest[:at], rest[at:]) if at >= 0 else (rest, "")
    at = authority.rfind("@")
    if at >= 0:
        authority = f"{REDACTED}@{authority[at + 1:]}"
    return scheme + authority + path + query + fragment


def _why(exc: Exception) -> str:
    """Why a download failed, in words of our own.

    Never the exception's text: ``requests`` and urllib3 both repeat the
    address in theirs (``404 Client Error: ... for url: ...``, ``Max retries
    exceeded with url: /gtfs.zip?api_key=...``), query and all.
    """
    if isinstance(exc, requests.HTTPError):
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if isinstance(status, int):
            try:
                return f"the server answered {status} {HTTPStatus(status).phrase}"
            except ValueError:
                return f"the server answered {status}"
    if isinstance(exc, requests.Timeout):
        return "the server did not answer in time"
    if isinstance(exc, requests.exceptions.SSLError):
        return "a secure connection could not be made"
    if isinstance(exc, requests.ConnectionError):
        return "the server could not be reached"
    if isinstance(exc, requests.TooManyRedirects):
        return "it redirected too many times"
    if isinstance(exc, (requests.exceptions.MissingSchema,
                        requests.exceptions.InvalidSchema,
                        requests.exceptions.InvalidURL)):
        return "the address is not one that can be fetched"
    return f"the request failed ({type(exc).__name__})"


class Interrupted(Exception):
    """An add(), a remove() or a download stopped because its caller asked: an
    add or a download kept nothing, and a remove left the feed as it was."""


def add(source: Path | str, *, key: str | None = None, name: str | None = None,
        mode: str = "all", agency: str | None = None,
        progress: "Callable[[str, int, int | None], None] | None" = None,
        cancelled: "Callable[[], bool] | None" = None,
        committing: Callable[[], bool] | None = None) -> Feed:
    """Add a feed a person chose, from a file or a URL, and keep it.

    The zip is fetched or copied into the cache, checked for the tables a
    map and an animation need, and recorded in ``user-feeds.json``. ``name``
    defaults to the first agency's name in the feed, ``key`` to a slug of the
    name made unique; a ``key`` given is checked for form and taken as is. A
    feed that fails the check leaves nothing behind. Raises FeedError with the
    sentence to show. ``progress(stage, done, total)`` hears the download's
    bytes (``total`` None when the server did not say) and then the check;
    ``cancelled()`` is asked between chunks and once more before the feed is
    kept, so a yes at any point raises Interrupted and leaves no feed in the
    registry and nothing on disk -- whichever source it came from.
    ``committing()`` is the caller's point of no return (issue 80), asked
    under the registry's lock and the feed's disk lock immediately before
    the zip is moved into the cache and the registry written: a no is a
    cancel, raised as Interrupted with nothing kept; after a yes the feed is
    kept whatever ``cancelled()`` says.
    """
    source_text = str(source)
    from_url = _is_url(source_text)
    # How the source is named in a sentence, and what the feed is called when
    # it names no agency: an address without its secrets, and the file's name
    # from the address's path alone, never from its query.
    what = shown(source_text) if from_url else Path(source_text).name
    try:
        fallback = Path(urlsplit(source_text).path if from_url else source_text).stem
    except ValueError:
        fallback = ""
    if key is not None and not KEY_PATTERN.fullmatch(key):
        raise FeedError("a feed key is lower-case letters, digits and hyphens, up to 64")
    if not valid_mode(mode):
        raise FeedError(f"{mode!r} is not a mode LOOM knows")

    staging = config.feeds_dir() / f".adding-{os.getpid()}-{threading.get_ident()}.zip"
    staging.parent.mkdir(parents=True, exist_ok=True)
    try:
        if from_url:
            _download(source_text, staging, progress=progress, cancelled=cancelled)
            url = source_text
        else:
            src = Path(source_text)
            if not src.is_file():
                raise FeedError(f"{what} is not a file")
            shutil.copyfile(src, staging)
            url = ""
        if progress is not None:
            progress("check", 0, None)
        members = _check_gtfs(staging, what)
        headways = _publishes_headways(staging, members, what)
        if progress is not None:
            progress("check", 1, None)
        if name is None:
            name = _agency_name(staging, members, what) or fallback or "Feed"
        if key is None:
            base = slug(name)
            key = base
            n = 2
            while key in FEEDS or key in user_feeds():
                key = f"{base[:60]}-{n}"
                n += 1
        # The last word before anything is kept. Asking only between chunks
        # left a cancel that arrived after the final chunk -- or during a
        # file copy, which asks nothing -- to fall through to the commit: the
        # caller was told the add was cancelled while the feed sat in the
        # registry. The ``finally`` below takes the staging file with it.
        if cancelled is not None and cancelled():
            raise Interrupted()
        with _user_lock:
            users = user_feeds()
            if key in FEEDS or key in users:
                raise FeedError(f"{key!r} is already a feed; choose another key")
            feed = Feed(key=key, name=name, url=url, mode=mode, agency=agency,
                        headways=headways, source="user")
            # Under the feed's disk lock as well, as remove's deletions are: a
            # removal of this key still deleting its files would otherwise
            # delete the zip just kept, from the list it made before.
            with _disk(key):
                # The point of no return, in the hold of both locks, so a
                # cancel that came while the add waited for either -- a
                # removal of the key can hold the disk lock for seconds -- is
                # honoured too, and none falls between the look and the
                # keeping (issue 80).
                if committing is not None and not committing():
                    raise Interrupted()
                os.replace(staging, feed.zip_path)
            users[key] = feed
            _write_user_feeds(users)
        return feed
    finally:
        staging.unlink(missing_ok=True)


def remove(key: str, *, cancelled: "Callable[[], bool] | None" = None,
           forgotten: "Callable[[], None] | None" = None) -> None:
    """Forget a feed a person added, with its zips and its stored layouts.
    A preset cannot be removed.

    The registry's write is the point of no return, and it is atomic. Until
    it, ``cancelled()`` has the last word: a yes raises Interrupted and the
    feed stays registered with every file in place. Once it is written the
    feed is forgotten, ``forgotten()`` is told, and the zips and the layouts
    are removed to the end whatever ``cancelled()`` says, because stopping
    there would leave files no registry entry names and nothing would ever
    remove. Only the feed's own zips under the feeds folder, the folders the
    native backend unpacked them into (``<key>.normalized`` and its scratch
    ``.unpacking``), and its own folder under the graphs folder are removed.
    """
    if not KEY_PATTERN.fullmatch(key):
        raise FeedError("a feed key is lower-case letters, digits and hyphens, up to 64")
    if key in FEEDS:
        raise FeedError(f"{key!r} is a built-in feed and cannot be removed")
    with _user_lock:
        users = user_feeds()
        if key not in users:
            raise FeedError(f"{key!r} is not a registered feed")
        if cancelled is not None and cancelled():
            raise Interrupted()
        del users[key]
        _write_user_feeds(users)
        if forgotten is not None:
            forgotten()
    with _disk(key):
        # In an order the filesystem does not choose, so a stop partway, or a
        # test that holds one removal, means the same thing everywhere.
        for path in sorted(config.feeds_dir().glob(f"{key}.*zip")):
            path.unlink(missing_ok=True)
        # What ``loom.unpacked`` made beside a zip for the native backend, and
        # its scratch while it runs: folders, ending as it names them, and
        # nothing else that begins with the key.
        for path in sorted(config.feeds_dir().glob(f"{key}.*")):
            if (path.is_dir() and not path.is_symlink()
                    and path.name.endswith((".normalized", ".normalized.unpacking"))):
                shutil.rmtree(path, ignore_errors=True)
        # The layouts under the store's lock, which every move of a stored
        # set and every read of one takes (engine issue 83): never between a
        # forced re-layout's two renames, nor while a reader holds a file of
        # a set open, which Windows would not delete. For the deletion alone,
        # which waits on no cancel.
        from . import pipeline  # here, not at the top: pipeline reads this module
        with pipeline._lock:
            shutil.rmtree(config.graphs_dir() / key, ignore_errors=True)


def inspect(key: str, *, anchor: "dt.date | None" = None):
    """What is in ``key``'s feed, as data: see ``schematic.inspection``."""
    from . import inspection  # here, not at the top: inspection reads this module
    return inspection.inspect(key, anchor=anchor)


def _download(url: str, dest: Path, *, named: str | None = None,
              progress: "Callable[[str, int, int | None], None] | None" = None,
              cancelled: "Callable[[], bool] | None" = None) -> Path:
    """``url``'s bytes into ``dest``, streamed so a caller can watch and stop
    it, or a FeedError; a page that is not a zip is refused and removed, and
    so is a download past ``MAX_DOWNLOAD_BYTES``, stopped at the limit.
    ``named`` is how a refusal names the address: without its secrets unless
    the caller says otherwise, as it does for a preset's public one."""
    named = shown(url) if named is None else named
    over = (f"{named} could not be fetched: it is larger than "
            f"{_size(MAX_DOWNLOAD_BYTES)}, the most a feed may be to download; {NARROWER}")
    # Some agencies (MARTA) return 403 to a bare requests user-agent.
    resp = None
    try:
        resp = requests.get(url, timeout=180, stream=True, headers={
            "User-Agent": "OpenSchematicMaps/0.1 (+https://github.com/)",
        })
        resp.raise_for_status()
        length = resp.headers.get("Content-Length") if hasattr(resp, "headers") else None
        total = int(length) if length and str(length).isdigit() else None
        # A length the server states is believed when it is too large, and
        # never when it is not: the count below is what holds the limit.
        if total is not None and total > MAX_DOWNLOAD_BYTES:
            raise FeedError(over)
        done = 0
        with open(dest, "wb") as out:
            for chunk in resp.iter_content(1 << 16):
                if cancelled is not None and cancelled():
                    raise Interrupted()
                done += len(chunk)
                if done > MAX_DOWNLOAD_BYTES:
                    break
                out.write(chunk)
                if progress is not None:
                    progress("download", done, total)
        if done > MAX_DOWNLOAD_BYTES:
            dest.unlink(missing_ok=True)
            raise FeedError(over)
    except requests.RequestException as exc:
        dest.unlink(missing_ok=True)
        # No cause carried: the traceback a failed request is logged with
        # would print the exception's own text, which names the address whole.
        raise FeedError(f"{named} could not be fetched: {_why(exc)}") from None
    finally:
        # A cancelled download's connection goes now, not at collection. A
        # cancel is asked between chunks, so on a server that has stopped
        # sending it waits for the next chunk or the read timeout.
        close = getattr(resp, "close", None)
        if close is not None:
            close()
    # Fail loudly rather than caching an HTML error page as a "feed".
    if not zipfile.is_zipfile(dest):
        size = dest.stat().st_size
        dest.unlink(missing_ok=True)
        raise FeedError(f"{named} did not return a zip ({size} bytes)")
    return dest


# One feed on disk at a time, per key: two requests for one feed (the service
# day and the layout, back to back) must not download or normalise it twice
# into the same file, and a download must land whole or not at all.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _disk(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


# Who hears a download that happens inside a request, per thread (E36). A
# preset's zip is fetched the first time anything needs it, deep inside a
# layout, an inspection or a service-day read, through callers that know
# nothing of the request; the request's own thread says, once, who wants the
# bytes and who may stop them, as ``loom.cancellable`` does for a tool.
_watch = threading.local()

DownloadProgress = Callable[[str, int, "int | None"], None]


@contextmanager
def watched(progress: DownloadProgress | None,
            cancelled: Callable[[], bool] | None) -> Iterator[None]:
    """Run the block with any download ``fetch`` makes reporting its bytes to
    ``progress(stage, done, total)`` and asking ``cancelled()`` between chunks
    and once more before the zip is kept."""
    previous = getattr(_watch, "hooks", None)
    _watch.hooks = (progress, cancelled)
    try:
        yield
    finally:
        _watch.hooks = previous


def fetch(key: str, *, force: bool = False) -> Path:
    """Download a feed if it is not already cached. Returns the local zip path.

    Inside ``watched`` the download reports its bytes and can be cancelled:
    a cancel raises Interrupted and caches nothing, so the next call
    downloads afresh. A feed already cached reports nothing.
    """
    feed = get(key)
    feed.zip_path.parent.mkdir(parents=True, exist_ok=True)
    progress, cancelled = getattr(_watch, "hooks", None) or (None, None)
    with _disk(key):
        if feed.zip_path.exists() and not force:
            return feed.zip_path
        if not feed.url:
            raise FeedError(f"{key!r} was added from a file and its zip is gone; add it again")
        # Whole or not at all: a quit mid-write must not leave a truncated
        # zip that the next call takes for the feed, and neither may a cancel,
        # a refusal or a failure (the .part goes with any of them).
        partial = feed.zip_path.with_name(feed.zip_path.name + ".part")
        try:
            # A request that waited here for another's download, which was
            # cancelled and kept nothing, may have been cancelled itself
            # meanwhile: the lock's wait cannot be interrupted, so ask now,
            # before a byte is fetched.
            if cancelled is not None and cancelled():
                raise Interrupted()
            _download(feed.url, partial, named=feed.shown_url,
                      progress=progress, cancelled=cancelled)
            # The last word, as ``add`` has it: a cancel after the final chunk
            # is still a cancel, and nothing is kept.
            if cancelled is not None and cancelled():
                raise Interrupted()
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        os.replace(partial, feed.zip_path)
        return feed.zip_path


# --------------------------------------------------------------------------
# Normalisation
# --------------------------------------------------------------------------
#
# ``gtfs2graph`` labels each line with ``route_short_name``. Plenty of feeds --
# LA Metro among them -- leave that column blank, which yields a graph whose
# lines are all labelled "". Since the label is the only handle we have on a
# route downstream, fill it in before LOOM ever sees the feed, and derive the
# schedule side's labels from the same function so the two cannot disagree.


# What gtfs2graph's -m accepts, by name; a numeric route_type does too. A
# registry entry joins several with commas ("tram,subway").
MOTS = frozenset({"all", "tram", "streetcar", "subway", "metro", "rail", "train", "bus",
                  "ferry", "boat", "ship", "cablecar", "gondola", "funicular", "coach",
                  "mono-rail", "monorail", "trolley", "trolleybus", "trolley-bus"})


def valid_mode(value: str) -> bool:
    """One or more MOTs, comma-joined, each a name LOOM knows or a route_type."""
    parts = value.split(",")
    # builtins.all, not the registry's all() defined above.
    return bool(value) and builtins.all(part in MOTS or part.isdigit() for part in parts)


# The registry fields a caller may override for one build: what LOOM keeps
# (``mode``), which operator (``agency``) and how a line is labelled. Together
# with the feed's bytes and the LOOM build they are what names a layout
# (``pipeline.layout_inputs``).
OVERRIDES = ("mode", "agency", "label_pattern", "label_strip")


# The one way to ask for no agency at all on a feed whose entry names one:
# None means the registry's value, so "none" needs a word of its own.
NO_AGENCY = ""


def resolved(key: str, **overrides: Any) -> Feed:
    """The registry entry with any of ``OVERRIDES`` replaced. ``None`` means
    the registry's value; an override equal to it changes nothing; an
    ``agency`` of ``NO_AGENCY`` (the empty string) means every operator,
    whatever the entry says."""
    unknown = set(overrides) - set(OVERRIDES)
    if unknown:
        raise TypeError(f"not something a build can override: {', '.join(sorted(unknown))}")
    feed = get(key)
    given = {name: value for name, value in overrides.items() if value is not None}
    if given.get("agency") == NO_AGENCY:
        given["agency"] = None
    return replace(feed, **given) if given else feed


def variant(feed: Feed) -> str | None:
    """How ``feed`` differs from its registry entry, as a short token that keys
    its normalised copy on disk; None when it does not differ, so the copy the
    site and the notebooks have always used keeps its name."""
    base = get(feed.key)
    diff = {name: getattr(feed, name) for name in OVERRIDES
            if getattr(feed, name) != getattr(base, name)}
    if not diff:
        return None
    return hashlib.sha256(json.dumps(diff, sort_keys=True).encode()).hexdigest()[:12]


def normalized_path(feed: Feed) -> Path:
    """Where ``feed``'s normalised copy lives: the registry's name, or the
    variant's."""
    token = variant(feed)
    if token is None:
        return feed.normalized_zip_path
    return config.feeds_dir() / f"{feed.key}.{token}.normalized.zip"


def _feed(feed_or_key: Feed | str) -> Feed:
    return feed_or_key if isinstance(feed_or_key, Feed) else get(feed_or_key)


def route_labels(feed_or_key: Feed | str, routes: pd.DataFrame) -> pd.Series:
    """The label each route should carry, indexed like ``routes``."""
    feed = _feed(feed_or_key)
    short = routes.get("route_short_name")
    long = routes.get("route_long_name")

    def pick(i: int) -> str:
        s = short.iloc[i] if short is not None else None
        if isinstance(s, str) and s.strip():
            label = s.strip()
        else:
            l = long.iloc[i] if long is not None else None
            if isinstance(l, str) and l.strip():
                l = l.strip()
                m = re.match(feed.label_pattern, l) if feed.label_pattern else None
                label = m.group(1) if m else l
            else:
                label = str(routes["route_id"].iloc[i])
        if feed.label_strip:
            stripped = re.sub(feed.label_strip, "", label).strip()
            if stripped:
                label = stripped
        return label

    return pd.Series([pick(i) for i in range(len(routes))], index=routes.index)


def normalize(feed_or_key: Feed | str, *, force: bool = False) -> Path:
    """Write a copy of the feed with ``route_short_name`` filled in.

    Returns the path to the normalised zip, which is what should be handed to
    ``gtfs2graph``. A ``Feed`` with overrides gets a copy of its own, named by
    ``variant``, so two builds with different agencies never share one.
    """
    feed = _feed(feed_or_key)
    key = feed.key
    src = fetch(key)
    dst = normalized_path(feed)
    with _disk(key):
        if dst.exists() and not force and dst.stat().st_mtime >= src.stat().st_mtime:
            return dst
        return _normalize(feed, src, dst)


def _entry(name: str) -> zipfile.ZipInfo:
    """A member as ``ZipFile.writestr`` makes one from a name: stamped now,
    deflated, readable and writable by its owner."""
    info = zipfile.ZipInfo(name, date_time=time.localtime(time.time())[:6])
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _normalize(feed: Feed, src: Path, dst: Path) -> Path:
    """The work of ``normalize``: read ``src``, write ``dst`` whole. Every
    table is read through the limits a piece at a time, and one the agency
    filter leaves alone is copied across the same way, never held whole."""
    what = repr(feed.key)
    budget = _Budget(what)

    def read(zf: zipfile.ZipFile, name: str) -> pd.DataFrame:
        with _member(zf, name, budget) as fh:
            df = pd.read_csv(fh, dtype=str, skipinitialspace=True)
        df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
        return df

    with _opened(src, what) as zin:
        names = [n for n in zin.namelist()
                 if n.endswith(".txt") and Path(n).stem in GTFS_TABLES
                 and Path(n).stem not in LOOM_SKIP]
        routes = read(zin, "routes.txt")
        routes["route_short_name"] = route_labels(feed, routes)

        # Rewritten tables, by stem. Only what the agency filter touches.
        rewritten: dict[str, pd.DataFrame] = {"routes": routes}
        if feed.agency and "agency_id" in routes.columns:
            # Dropping routes leaves orphan trips and stop_times behind, which
            # is worse than not filtering, so cascade through the references.
            rewritten["routes"] = routes = routes[routes["agency_id"] == feed.agency]
            keep_routes = set(routes["route_id"])
            if any(Path(n).stem == "trips" for n in names):
                trips = read(zin, "trips.txt")
                trips = trips[trips["route_id"].isin(keep_routes)]
                rewritten["trips"] = trips
                keep_trips = set(trips["trip_id"])
                for stem in ("stop_times", "frequencies"):
                    match = next((n for n in names if Path(n).stem == stem), None)
                    if match:
                        df = read(zin, match)
                        rewritten[stem] = df[df["trip_id"].isin(keep_trips)]

        def encode(df: pd.DataFrame) -> bytes:
            buf = io.StringIO()
            # LF, not pandas' os.linesep default, so the normalised copy is the
            # same bytes on every platform. (A layout's id hashes the raw zip,
            # not this copy.)
            df.to_csv(buf, index=False, lineterminator="\n")
            return buf.getvalue().encode()

        tmp = dst.with_suffix(".tmp")
        try:
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
                for name in names:
                    stem = Path(name).stem
                    if stem in rewritten:
                        zout.writestr(name, encode(rewritten[stem]))
                        continue
                    with _member(zin, name, budget) as fh, zout.open(_entry(name), "w") as out:
                        shutil.copyfileobj(fh, out, _PIECE)
        except BaseException:
            # A refusal partway, or a member that fails its CRC, leaves no
            # copy half written beside the zips.
            tmp.unlink(missing_ok=True)
            raise
    shutil.move(tmp, dst)
    return dst


def tables(feed_or_key: Feed | str, *, normalized: bool = True,
           only: frozenset[str] | set[str] | None = None) -> dict[str, pd.DataFrame]:
    """Read every .txt in the feed zip as a DataFrame, keyed by table name.

    Reads the normalised feed by default so route labels match the line graph;
    a ``Feed`` with overrides reads the copy normalised with them. ``only``
    names the tables to read, for a caller that does not need stop_times.
    """
    feed = _feed(feed_or_key)
    path = normalize(feed) if normalized else fetch(feed.key)
    out: dict[str, pd.DataFrame] = {}
    what = repr(feed.key)
    with _opened(path, what) as zf:
        budget = _Budget(what)
        for name in zf.namelist():
            stem = Path(name).stem
            if not name.endswith(".txt") or stem not in GTFS_TABLES:
                continue
            if only is not None and stem not in only:
                continue
            with _member(zf, name, budget) as fh:
                # skipinitialspace and the header strip handle feeds that pad
                # their CSV with spaces after commas -- Metra's headers come
                # through as " trip_id", which breaks every join downstream.
                df = pd.read_csv(fh, dtype=str, low_memory=False, skipinitialspace=True)
            df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
            out[stem] = df
    return out
