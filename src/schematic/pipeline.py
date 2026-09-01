"""End-to-end pipeline: GTFS feed in, schematic map and animation out.

Each stage caches its LOOM output under ``data/graphs/<feed>/`` so a notebook can
re-run a later stage without paying for the earlier ones -- ``gtfs2graph`` takes
about ten seconds, everything after it is instant.

    from schematic import pipeline
    result = pipeline.run("la-metro-rail")
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path

from . import animate, feeds, loom
from .crs import to_mercator
from .linegraph import LineGraph
from .render import RenderResult, Style, octilinearity, render
from .schedule import (StopMatch, Trip, busiest_weekday, match_stops, trips_on)

GRAPH_DIR = feeds.DATA_DIR / "graphs"
OUT_DIR = feeds.REPO_ROOT / "out"

# The LOOM stages, in order, with the arguments we run them with. Kept as data
# so a notebook can print the pipeline or re-run one stage with a tweak.
STAGES: list[tuple[str, tuple[str, ...]]] = [
    ("topo", ()),
    ("loom", ()),
    ("octi", ()),
]


def graph_dir(key: str) -> Path:
    d = GRAPH_DIR / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def line_graph(key: str, *, force: bool = False) -> Path:
    """Stage 0: GTFS -> LOOM line graph. Cached; this is the slow step."""
    out = graph_dir(key) / "00_gtfs2graph.json"
    if out.exists() and not force:
        return out
    feed = feeds.FEEDS[key]
    graph = loom.gtfs2graph(feeds.normalize(key), "-m", feed.mode)
    out.write_text(json.dumps(graph))
    return out


def schematize(key: str, *, force: bool = False,
               stages: list[tuple[str, tuple[str, ...]]] | None = None) -> dict[str, Path]:
    """Run topo | loom | octi, caching each stage. Returns stage name -> path."""
    stages = stages or STAGES
    d = graph_dir(key)
    paths = {"gtfs2graph": line_graph(key, force=force)}
    payload = paths["gtfs2graph"].read_bytes()
    for i, (tool, args) in enumerate(stages, start=1):
        out = d / f"{i:02d}_{tool}.json"
        if out.exists() and not force:
            payload = out.read_bytes()
        else:
            payload = json.dumps(loom.run(tool, payload, *args)).encode()
            out.write_bytes(payload)
        paths[tool] = out
    return paths


@dataclass
class Result:
    key: str
    date: dt.date
    graph: LineGraph          # projected, ready to draw
    render: RenderResult
    trips: list[Trip]
    match: StopMatch
    animation: animate.Animation
    paths: dict[str, Path]

    def summary(self) -> str:
        ok, total = octilinearity(self.graph)
        peak = max(len(self.animation.trips) and
                   sum(1 for t in self.animation.trips
                       if t["k"][0][0] <= h * 3600 <= t["k"][-1][0])
                   for h in range(24))
        return "\n".join([
            f"{feeds.FEEDS[self.key].name} -- {self.date:%A %d %B %Y}",
            f"  {self.graph.summary()}",
            f"  octilinear: {100 * ok / total:.1f}% of drawn length",
            f"  stops: {self.match.report()}",
            f"  trips: {len(self.trips)} routed onto {len(self.animation.paths)} distinct paths"
            + (f", {len(self.animation.unrouted)} UNROUTED" if self.animation.unrouted else ""),
            f"  degraded: {self.animation.trips_with_skipped_calls} trips skipped an unmatched stop, "
            f"{self.animation.trips_with_borrowed_track} borrowed another line's track",
            f"  labels dropped: {len(self.render.dropped_labels)}",
            f"  peak concurrent trains: {peak}",
        ])


def run(key: str, *, date: dt.date | None = None, width: float = 1800.0,
        style: Style | None = None, line_order: list[str] | None = None,
        force: bool = False, out_dir: Path | None = None,
        back: str = "index.html", icons: str | None = None) -> Result:
    """Everything: fetch, schematize, draw, schedule, animate, write.

    ``back`` is the href the animation page's back-link points at. The default
    is the sibling gallery in ``out/``; the site passes its own atlas URL,
    because a relative "index.html" resolves to /maps/index.html there.
    """
    paths = schematize(key, force=force)

    # Match stops against the unprojected graph -- station_id is what matters
    # there, and reprojecting is only needed for geometry.
    graph_ll = LineGraph.from_geojson(paths["octi"])
    if not graph_ll.edges:
        raise ValueError(
            f"{key}: the line graph is empty -- gtfs2graph -m {feeds.FEEDS[key].mode!r} "
            f"matched no routes. Check the feed's route_type values; agencies "
            f"disagree about which of tram/subway/rail their network is.")
    graph = graph_ll.reproject(to_mercator)

    tables = feeds.tables(key)
    lines = set(graph_ll.labels)
    match = match_stops(graph_ll, tables)
    date = date or busiest_weekday(tables, lines)
    trips = trips_on(tables, date, match, lines)

    name = feeds.FEEDS[key].name
    # Themed by default: the CSS variables carry literal fallbacks, so a
    # standalone SVG is unchanged, while an embedding page can theme the
    # furniture without resorting to an invert filter over the line colours.
    r = render(graph, width=width, style=style or Style(themed=True),
               title=name, line_order=line_order)
    # The loom stage, not gtfs2graph: same stations and the same solved line
    # ordering, so only the shape differs. See animate.geographic_tracks.
    geo = None
    if feeds.FEEDS[key].geographic:
        geo_graph = LineGraph.from_geojson(paths["loom"]).reproject(to_mercator)
        geo = animate.geographic_tracks(geo_graph, graph, r)

    anim = animate.build(r, graph, trips, date, geo, line_order=line_order)

    out = out_dir or OUT_DIR
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{key}.svg").write_text(r.svg)
    animate.write(anim, r.svg, out, stem=key, back=back, icons=icons,
                  title=f"{name} — {date:%A %-d %B %Y}", name=name,
                  subtitle=f"{len(trips):,} trips · {date:%A %-d %B %Y}")

    return Result(key=key, date=date, graph=graph, render=r, trips=trips,
                  match=match, animation=anim, paths=paths)
