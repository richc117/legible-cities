"""Export built maps and their provenance into the Eleventy site.

The site's ``networks.json`` is generated, never hand-written, so the numbers on
the atlas page cannot drift from the maps beside them. Everything here reuses
``pipeline.run``; nothing about a city is described twice.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass
from pathlib import Path

from . import feeds, pipeline
from .crs import to_mercator
from .linegraph import LineGraph
from .render import Style, render

SITE_DIR = feeds.REPO_ROOT / "site"
SRC_DIR = SITE_DIR / "src"
MAPS_DIR = SRC_DIR / "maps"
DATA_DIR = SRC_DIR / "_data"

# The essay's worked example. Its geographic counterpart is exported too, for
# the before/after that carries the Beck section.
FEATURED = "la-metro-rail"

# Where an animation page's back-link goes. Absolute: the pages live at
# /maps/<city>.html, so a relative "index.html" would resolve to /maps/.
ATLAS_URL = "/atlas/"

# Networks the essay shows without labels, to compare their shapes. At the size
# a side-by-side figure allows, station names are unreadable anyway, and having
# them on one map but not the other would make the two incomparable.
PLAIN = (FEATURED, "cdmx-metro")


@dataclass
class NetworkEntry:
    """One city, as the site's templates need it."""

    key: str
    name: str
    stations: int
    lines: list[str]
    trips: int
    date: str
    svg: str
    animation: str
    feed_url: str
    # Anything the pipeline had to fudge, stated rather than buried.
    caveats: list[str]
    # Facts about the feed itself, which no amount of computing would reveal.
    notes: list[str]


def _caveats(result: pipeline.Result) -> list[str]:
    """What the pipeline had to fudge, phrased so a reader knows what it means.

    A bare count is alarming without being informative -- "6,627 trips" sounds
    catastrophic until you know it means a quarter of the hops are drawn one
    track over. Each line here says the consequence, not just the number.
    """
    out: list[str] = []
    m, a = result.match, result.animation
    trips = max(len(result.trips), 1)

    if m.unmatched:
        total = len(m.stop_to_node) + len(m.unmatched)
        out.append(f"{len(m.unmatched):,} of {total:,} stops could not be placed "
                   f"on the map, so trains pass straight through them")
    if a.unrouted:
        out.append(f"{len(a.unrouted):,} trips could not be traced across the "
                   f"network at all and are not shown")
    if a.trips_with_skipped_calls:
        pct = 100 * a.trips_with_skipped_calls / trips
        out.append(f"{a.trips_with_skipped_calls:,} trips ({pct:.0f}%) skip a "
                   f"stop the map does not carry")
    if a.trips_with_borrowed_track:
        pct = 100 * a.trips_with_borrowed_track / trips
        out.append(f"{a.trips_with_borrowed_track:,} trips ({pct:.0f}%) run part "
                   f"of the way on a neighbouring line's track, because the "
                   f"schematiser did not attribute that segment to their line. "
                   f"They follow the right corridor, but not always the right "
                   f"parallel track")
    if result.render.dropped_labels:
        n = len(result.render.dropped_labels)
        out.append(f"{n:,} station name{'s' if n > 1 else ''} had nowhere to sit "
                   f"without overlapping another and {'are' if n > 1 else 'is'} "
                   f"not drawn")
    return out


def export_unlabelled(key: str, stage: str, name: str,
                      width: float = 1100.0) -> Path:
    """Draw one pipeline stage of a network with no station names.

    The renderer is generic over any line graph, so this is the same code path
    that draws the finished map, pointed at an earlier stage or told to leave
    the labels off.
    """
    graph = LineGraph.from_geojson(pipeline.GRAPH_DIR / key / stage)
    svg = render(graph.reproject(to_mercator), width=width,
                 style=Style(themed=True), labels=False).svg
    path = MAPS_DIR / name
    path.write_text(svg)
    return path


def export_comparison(key: str, width: float = 1100.0) -> tuple[Path, Path]:
    """The before and after that carries the essay's Beck section.

    The geographic side comes straight off the cached gtfs2graph stage, so the
    pair differs only in whether ``octi`` has run.
    """
    return (export_unlabelled(key, "00_gtfs2graph.json", f"{key}-geographic.svg", width),
            export_unlabelled(key, "03_octi.json", f"{key}-plain.svg", width))


def export(keys: list[str] | None = None, *, width: float = 1600.0) -> list[NetworkEntry]:
    """Build every city and copy its artifacts into the site."""
    keys = keys or list(feeds.FEEDS)
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    entries: list[NetworkEntry] = []
    for key in keys:
        result = pipeline.run(key, width=width, out_dir=MAPS_DIR, back=ATLAS_URL)
        entries.append(NetworkEntry(
            key=key,
            name=feeds.FEEDS[key].name,
            stations=len(result.graph.stations),
            lines=result.graph.labels,
            trips=len(result.trips),
            date=result.date.isoformat(),
            svg=f"{key}.svg",
            animation=f"{key}.html",
            feed_url=feeds.FEEDS[key].url,
            caveats=_caveats(result),
            notes=list(feeds.FEEDS[key].notes),
        ))

    export_comparison(FEATURED)
    for key in PLAIN:
        if key != FEATURED and (pipeline.GRAPH_DIR / key / "03_octi.json").exists():
            export_unlabelled(key, "03_octi.json", f"{key}-plain.svg")

    # Largest first: the atlas should open on New York.
    entries.sort(key=lambda e: -e.stations)
    payload = {
        "generated": dt.date.today().isoformat(),
        "totals": {
            "networks": len(entries),
            "stations": sum(e.stations for e in entries),
            "trips": sum(e.trips for e in entries),
        },
        "featured": FEATURED,
        "networks": [asdict(e) for e in entries],
    }
    (DATA_DIR / "networks.json").write_text(json.dumps(payload, indent=2) + "\n")
    return entries
