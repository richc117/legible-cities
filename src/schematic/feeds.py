"""GTFS feed registry and local cache.

Feeds are downloaded once into ``data/feeds`` and reused. Add a city by adding a
``Feed`` to ``FEEDS`` -- nothing downstream in the pipeline is city-specific.
"""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data"
FEED_DIR = DATA_DIR / "feeds"


@dataclass(frozen=True)
class Feed:
    """A GTFS feed we know how to fetch."""

    key: str
    name: str
    url: str
    # LOOM's -m flag: tram, bus, coach, rail, subway, ferry, funicular, gondola, all
    mode: str = "all"
    # Applied to route_long_name when route_short_name is blank; group 1 becomes
    # the line label drawn on the map. See ``route_labels`` for why this matters.
    label_pattern: str | None = None
    # Removed from the label unconditionally. Some agencies publish one route per
    # direction (BART's "Yellow-N" / "Yellow-S"), which would otherwise draw
    # every line twice, side by side, as two separate colours of the same hue.
    label_strip: str | None = None

    @property
    def zip_path(self) -> Path:
        return FEED_DIR / f"{self.key}.zip"

    @property
    def normalized_zip_path(self) -> Path:
        return FEED_DIR / f"{self.key}.normalized.zip"


FEEDS: dict[str, Feed] = {
    "la-metro-rail": Feed(
        key="la-metro-rail",
        name="LA Metro Rail",
        url="https://gitlab.com/LACMTA/gtfs_rail/-/raw/master/gtfs_rail.zip",
        mode="all",
        # LA leaves route_short_name blank and names routes "Metro A Line".
        label_pattern=r"^Metro\s+(\S+)\s+Line$",
    ),
    "bart": Feed(
        key="bart",
        name="BART",
        url="https://www.bart.gov/dev/schedules/google_transit.zip",
        mode="all",
        # BART splits each line by direction: Yellow-N, Yellow-S, ...
        label_strip=r"-[NSEW]$",
    ),
    "trimet-max": Feed(
        key="trimet-max",
        name="TriMet MAX",
        url="https://developer.trimet.org/schedule/gtfs.zip",
        # TriMet publishes bus and rail in one feed; MAX is light rail.
        mode="tram",
    ),
}


def fetch(key: str, *, force: bool = False) -> Path:
    """Download a feed if it is not already cached. Returns the local zip path."""
    feed = FEEDS[key]
    feed.zip_path.parent.mkdir(parents=True, exist_ok=True)
    if feed.zip_path.exists() and not force:
        return feed.zip_path

    resp = requests.get(feed.url, timeout=120)
    resp.raise_for_status()
    # Fail loudly rather than caching an HTML error page as a "feed".
    if not zipfile.is_zipfile(io.BytesIO(resp.content)):
        raise RuntimeError(f"{feed.url} did not return a zip ({len(resp.content)} bytes)")
    feed.zip_path.write_bytes(resp.content)
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


def route_labels(key: str, routes: pd.DataFrame) -> pd.Series:
    """The label each route should carry, indexed like ``routes``."""
    feed = FEEDS[key]
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


def normalize(key: str, *, force: bool = False) -> Path:
    """Write a copy of the feed with ``route_short_name`` filled in.

    Returns the path to the normalised zip, which is what should be handed to
    ``gtfs2graph``.
    """
    feed = FEEDS[key]
    src = fetch(key)
    dst = feed.normalized_zip_path
    if dst.exists() and not force and dst.stat().st_mtime >= src.stat().st_mtime:
        return dst

    with zipfile.ZipFile(src) as zin:
        names = zin.namelist()
        routes = pd.read_csv(io.BytesIO(zin.read("routes.txt")), dtype=str)
        routes["route_short_name"] = route_labels(key, routes)
        buf = io.StringIO()
        routes.to_csv(buf, index=False)
        patched = buf.getvalue().encode()

        tmp = dst.with_suffix(".tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for name in names:
                zout.writestr(name, patched if name.endswith("routes.txt") else zin.read(name))
    shutil.move(tmp, dst)
    return dst


def tables(key: str, *, normalized: bool = True) -> dict[str, pd.DataFrame]:
    """Read every .txt in the feed zip as a DataFrame, keyed by table name.

    Reads the normalised feed by default so route labels match the line graph.
    """
    path = normalize(key) if normalized else fetch(key)
    out: dict[str, pd.DataFrame] = {}
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not name.endswith(".txt"):
                continue
            with zf.open(name) as fh:
                out[Path(name).stem] = pd.read_csv(fh, dtype=str, low_memory=False)
    return out
