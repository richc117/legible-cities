"""Export built maps and their provenance into the Eleventy site.

The site's ``networks.json`` is generated, never hand-written, so the numbers on
the atlas page cannot drift from the maps beside them. Everything here reuses
``pipeline.run``; nothing about a city is described twice.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from . import feeds, pipeline
# By name, not as a module: this file already has an export() of its own, and
# `from . import export` would be shadowed by it.
from .export import PALETTES, padded_box, resolve
from .crs import to_mercator
from .linegraph import LineGraph
from .render import Style, render

SITE_DIR = feeds.REPO_ROOT / "site"
SRC_DIR = SITE_DIR / "src"
MAPS_DIR = SRC_DIR / "maps"
DATA_DIR = SRC_DIR / "_data"
# Tracked, unlike MAPS_DIR: .gitignore drops site/src/maps wholesale, and the
# share card has to survive a fresh clone or the site deploys without one.
ASSETS_DIR = SRC_DIR / "assets"

# The essay's worked example. Its geographic counterpart is exported too, for
# the before/after that carries the Beck section.
FEATURED = "la-metro-rail"

# The share card: what Slack, Teams and Messenger draw when a link is pasted.
# 1200x630 is the size every one of them asks for, and a raster because none of
# them will render an SVG.
CARD_NAME = "og-card.png"
CARD_SIZE = (1200, 630)

def path_prefix() -> str:
    """The subpath the site is served from, with both slashes.

    Read from the same file Eleventy reads, because the animation pages are
    generated here rather than templated, and a prefix that disagreed between
    the two would give a site whose internal links half work.
    """
    config = SRC_DIR / "_data" / "site.json"
    prefix = "/"
    if config.exists():
        prefix = json.loads(config.read_text()).get("pathPrefix") or "/"
    return "/" + prefix.strip("/") + "/" if prefix.strip("/") else "/"


# Where an animation page's back-link goes. Absolute: the pages live at
# <prefix>maps/<city>.html, so a relative "index.html" would resolve to maps/.
def atlas_url() -> str:
    return path_prefix() + "atlas/"


def icons_url() -> str:
    return path_prefix() + "assets/favicon"


def origin() -> str:
    """Scheme and host, no path. Empty when the site has no published home.

    Kept apart from ``pathPrefix`` because they are two halves of one fact --
    where the site lives, and where under it -- and only the sharing tags need
    both. Everything else on the site is happy with a root-relative path; an
    unfurler is not, because it fetches the image with no page to resolve it
    against.
    """
    config = SRC_DIR / "_data" / "site.json"
    if not config.exists():
        return ""
    return (json.loads(config.read_text()).get("origin") or "").rstrip("/")


def card_url() -> str:
    """The share card, absolutely. Empty if there is no origin to hang it on."""
    return origin() + path_prefix() + "assets/" + CARD_NAME if origin() else ""


def page_url(key: str) -> str:
    """Where an animation page will be served from, absolutely."""
    return origin() + path_prefix() + f"maps/{key}.html" if origin() else ""

_VIEWBOX = re.compile(r'viewBox="([-\d.]+) ([-\d.]+) ([-\d.]+) ([-\d.]+)"')


def _reframe(svg: str, box: tuple[float, float, float, float],
             size: tuple[int, int]) -> str:
    """Point the SVG at a new viewBox, and grow the backdrop to match.

    The second half is the part that is easy to miss. ``render.py`` pins
    ``#backdrop`` to the box it drew, so widening the viewBox alone leaves the
    new gutters transparent -- and a transparent PNG is composited onto white by
    every chat client, which is exactly the seam this card exists to avoid.
    """
    x, y, w, h = box
    svg = _VIEWBOX.sub(f'viewBox="{x:.2f} {y:.2f} {w:.2f} {h:.2f}"', svg, count=1)
    svg = re.sub(r'width="[\d.]+" height="[\d.]+"',
                 f'width="{size[0]}" height="{size[1]}"', svg, count=1)
    return re.sub(
        r'<rect id="backdrop"[^>]*?fill="([^"]*)"\s*/>',
        lambda m: (f'<rect id="backdrop" x="{x:.2f}" y="{y:.2f}" '
                   f'width="{w:.2f}" height="{h:.2f}" fill="{m.group(1)}"/>'),
        svg, count=1)


def og_card(key: str = FEATURED, *, size: tuple[int, int] = CARD_SIZE) -> Path | None:
    """Rasterise the share card from the unlabelled map, or explain why not.

    Drawn from ``<key>-plain.svg`` -- the schematic without station names --
    because a card renders about 360px wide in a chat window, where labels
    solved for a 1600px map are noise rather than information.

    Two things this must do that a plain ``bin/preview`` would not: resolve the
    themed variables first, since librsvg takes the ``var()`` fallback and would
    put a dark map on a white ground; and pad the box out to the card's aspect
    with ``padded_box``, which grows rather than crops, so no station is
    cut off to make the shape.

    Missing ``rsvg-convert`` is a warning, not a failure: the card is committed,
    so the only cost is that it is not refreshed.
    """
    src = MAPS_DIR / f"{key}-plain.svg"
    if not src.exists():
        print(f"  note: {src.name} is missing, so the share card was not "
              f"regenerated (it is committed; run site.export() to rebuild it)")
        return None

    svg = resolve(src.read_text(), PALETTES["dark"])
    match = _VIEWBOX.search(svg)
    if not match:
        print(f"  note: {src.name} has no viewBox; share card skipped")
        return None
    box = tuple(float(v) for v in match.groups())
    svg = _reframe(svg, padded_box(box, size[0] / size[1]), size)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    dest = ASSETS_DIR / CARD_NAME
    staged = ASSETS_DIR / f".{CARD_NAME}.svg"
    staged.write_text(svg)
    try:
        subprocess.run(["rsvg-convert", "-w", str(size[0]), "-h", str(size[1]),
                        "-f", "png", "-o", str(dest), str(staged)], check=True)
    except FileNotFoundError:
        print("  note: rsvg-convert is not installed, so the share card was not "
              "regenerated (brew install librsvg)")
        return None
    finally:
        staged.unlink(missing_ok=True)
    return dest


# --------------------------------------------------------------- share tags

# The animation pages are generated, not templated, so Eleventy's head never
# reaches them -- .eleventy.js copies them byte-for-byte on purpose. These are
# the same tags base.njk emits, built here for the same reason atlas_url() is.
_SOCIAL = """<link rel="canonical" href="{url}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{site}">
<meta property="og:title" content="{title}">
<meta property="og:description" content="{desc}">
<meta property="og:url" content="{url}">
<meta property="og:image" content="{image}">
<meta property="og:image:width" content="{w}">
<meta property="og:image:height" content="{h}">
<meta property="og:image:alt" content="{alt}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="{image}">"""


def _config() -> dict:
    config = SRC_DIR / "_data" / "site.json"
    return json.loads(config.read_text()) if config.exists() else {}


def social_tags(key: str) -> str:
    """The sharing block for one animation page. Empty without an origin.

    Empty is the right answer there rather than a relative URL: a page written
    for standalone use should not claim a home it does not have, and an unfurler
    handed a relative og:image shows no picture anyway.
    """
    if not origin():
        return ""
    config = _config()
    feed = feeds.FEEDS[key]
    esc = html.escape
    return _SOCIAL.format(
        url=esc(page_url(key)),
        site=esc(config.get("title", "Legible Cities")),
        title=esc(feed.name),
        desc=esc(f"{feed.city} {feed.network} running a real day's timetable, "
                 f"drawn from the agency's own published GTFS feed."),
        image=esc(card_url()),
        w=CARD_SIZE[0], h=CARD_SIZE[1],
        alt=esc(config.get("card", {}).get("alt", "")))


# Networks the essay shows without labels, to compare their shapes. At the size
# a side-by-side figure allows, station names are unreadable anyway, and having
# them on one map but not the other would make the two incomparable.
PLAIN = (FEATURED, "cdmx-metro")

# The two the essay argues from, in the order it argues them: Los Angeles is the
# city the whole piece is about, and Mexico City is the counter-example it ends
# on. They lead the atlas regardless of how cleanly they came out, because the
# atlas is the essay's appendix before it is a league table.
LEADS = (FEATURED, "cdmx-metro")


@dataclass
class NetworkEntry:
    """One city, as the site's templates need it."""

    key: str
    name: str
    # Split out for the presentation-mode link: `name` alone cannot make a
    # two-line title, since it is inconsistent about carrying the city.
    city: str
    network: str
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
    # Weighted share of the network the pipeline had to fudge; 0 is clean. What
    # the atlas is ordered by, after the two leads.
    issues: float


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


# What each class of imperfection costs a reader, relative to the others. An
# unplaced stop and an untraceable trip are structural -- the map is missing
# something the timetable has. A skipped call is a hole in one trip. Borrowed
# track is the mildest: the train follows the right corridor, one parallel
# track over, which at this scale is a few pixels. A dropped label costs a name,
# not a train.
ISSUE_WEIGHTS = {"unplaced": 3.0, "unrouted": 3.0, "skipped": 2.0,
                 "borrowed": 1.0, "unlabelled": 1.0}


def _issue_score(result: pipeline.Result) -> float:
    """How much of this network the pipeline had to fudge, as one number.

    Proportions, never counts: New York has more of everything, including
    stations, and ranking by raw totals would just re-sort the atlas by size.
    Zero means every stop placed, every trip traced on its own track, and every
    name drawn. Sorts the atlas, and is worth reading beside ``_caveats``, which
    says the same things in words.
    """
    m, a = result.match, result.animation
    trips = max(len(result.trips), 1)
    stops = max(len(m.stop_to_node) + len(m.unmatched), 1)
    stations = max(len(result.graph.stations), 1)
    fractions = {
        "unplaced": len(m.unmatched) / stops,
        "unrouted": len(a.unrouted) / trips,
        "skipped": a.trips_with_skipped_calls / trips,
        "borrowed": a.trips_with_borrowed_track / trips,
        "unlabelled": len(result.render.dropped_labels) / stations,
    }
    return sum(ISSUE_WEIGHTS[k] * v for k, v in fractions.items())


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
        result = pipeline.run(key, width=width, out_dir=MAPS_DIR,
                              back=atlas_url(), icons=icons_url(),
                              social=social_tags(key))
        entries.append(NetworkEntry(
            key=key,
            name=feeds.FEEDS[key].name,
            city=feeds.FEEDS[key].city,
            network=feeds.FEEDS[key].network,
            stations=len(result.graph.stations),
            lines=result.graph.labels,
            trips=len(result.trips),
            date=result.date.isoformat(),
            svg=f"{key}.svg",
            animation=f"{key}.html",
            feed_url=feeds.FEEDS[key].url,
            caveats=_caveats(result),
            notes=list(feeds.FEEDS[key].notes),
            issues=round(_issue_score(result), 4),
        ))

    export_comparison(FEATURED)
    for key in PLAIN:
        if key != FEATURED and (pipeline.GRAPH_DIR / key / "03_octi.json").exists():
            export_unlabelled(key, "03_octi.json", f"{key}-plain.svg")

    # After export_comparison, which is what draws the unlabelled map it reads.
    if FEATURED in keys:
        og_card()

    # The two the essay argues from first, then cleanest to messiest. Ordering
    # by size opened the atlas on New York, which is both the biggest network
    # and the one with the most caveats under it -- so the page led with its
    # worst-looking case and buried the maps that came out perfectly. Size is
    # still on every entry; it is just not what the page is sorted by.
    lead = {key: i for i, key in enumerate(LEADS)}
    entries.sort(key=lambda e: (lead.get(e.key, len(lead)), e.issues, -e.stations))
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
