"""GTFS feed registry and local cache.

Feeds are downloaded once into ``data/feeds`` under the engine's home (see
``config``) and reused. Add a city by adding a ``Feed`` to ``FEEDS`` -- nothing
downstream in the pipeline is city-specific.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import threading
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from . import config

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

    @property
    def zip_path(self) -> Path:
        return config.feeds_dir() / f"{self.key}.zip"

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
        url="http://web.mta.info/developers/data/nyct/subway/google_transit.zip",
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
        url="http://www.miamidade.gov/transit/googletransit/current/google_transit.zip",
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
        url="http://www.dart.org/transitdata/latest/google_transit.zip",
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
        url="http://web.mta.info/developers/data/lirr/google_transit.zip",
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


# One feed on disk at a time, per key: two requests for one feed (the service
# day and the layout, back to back) must not download or normalise it twice
# into the same file, and a download must land whole or not at all.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _disk(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def fetch(key: str, *, force: bool = False) -> Path:
    """Download a feed if it is not already cached. Returns the local zip path."""
    feed = FEEDS[key]
    feed.zip_path.parent.mkdir(parents=True, exist_ok=True)
    with _disk(key):
        if feed.zip_path.exists() and not force:
            return feed.zip_path

        # Some agencies (MARTA) return 403 to a bare requests user-agent.
        resp = requests.get(feed.url, timeout=180, headers={
            "User-Agent": "OpenSchematicMaps/0.1 (+https://github.com/)",
        })
        resp.raise_for_status()
        # Fail loudly rather than caching an HTML error page as a "feed".
        if not zipfile.is_zipfile(io.BytesIO(resp.content)):
            raise RuntimeError(f"{feed.url} did not return a zip ({len(resp.content)} bytes)")
        # Whole or not at all: a quit mid-write must not leave a truncated
        # zip that the next call takes for the feed.
        partial = feed.zip_path.with_name(feed.zip_path.name + ".part")
        partial.write_bytes(resp.content)
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
    return bool(value) and all(part in MOTS or part.isdigit() for part in parts)


# The registry fields a caller may override for one build: what LOOM keeps
# (``mode``), which operator (``agency``) and how a line is labelled. Together
# with the feed's bytes and the LOOM build they are what names a layout
# (``pipeline.layout_inputs``).
OVERRIDES = ("mode", "agency", "label_pattern", "label_strip")


def resolved(key: str, **overrides: Any) -> Feed:
    """The registry entry with any of ``OVERRIDES`` replaced. ``None`` means
    the registry's value; an override equal to it changes nothing."""
    unknown = set(overrides) - set(OVERRIDES)
    if unknown:
        raise TypeError(f"not something a build can override: {', '.join(sorted(unknown))}")
    feed = FEEDS[key]
    given = {name: value for name, value in overrides.items() if value is not None}
    return replace(feed, **given) if given else feed


def variant(feed: Feed) -> str | None:
    """How ``feed`` differs from its registry entry, as a short token that keys
    its normalised copy on disk; None when it does not differ, so the copy the
    site and the notebooks have always used keeps its name."""
    base = FEEDS[feed.key]
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
    return feed_or_key if isinstance(feed_or_key, Feed) else FEEDS[feed_or_key]


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


def _normalize(feed: Feed, src: Path, dst: Path) -> Path:
    """The work of ``normalize``: read ``src``, write ``dst`` whole."""

    def read(zf: zipfile.ZipFile, name: str) -> pd.DataFrame:
        df = pd.read_csv(io.BytesIO(zf.read(name)), dtype=str, skipinitialspace=True)
        df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
        return df

    with zipfile.ZipFile(src) as zin:
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
            df.to_csv(buf, index=False)
            return buf.getvalue().encode()

        tmp = dst.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                stem = Path(name).stem
                zout.writestr(name, encode(rewritten[stem]) if stem in rewritten
                              else zin.read(name))
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
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            stem = Path(name).stem
            if not name.endswith(".txt") or stem not in GTFS_TABLES:
                continue
            if only is not None and stem not in only:
                continue
            with zf.open(name) as fh:
                # skipinitialspace and the header strip handle feeds that pad
                # their CSV with spaces after commas -- Metra's headers come
                # through as " trip_id", which breaks every join downstream.
                df = pd.read_csv(fh, dtype=str, low_memory=False, skipinitialspace=True)
            df.columns = [c.strip().lstrip("\ufeff") for c in df.columns]
            out[stem] = df
    return out
