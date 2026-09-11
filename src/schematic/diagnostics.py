"""What a build had to fudge, as data, and the two renderings of it.

``Result.summary()`` printed a string, the server built the same numbers as
a dict for ``map.build``, and the site computed them a third time for the
atlas's caveats and its issue score. One ``Result`` in three renderings,
none shared. This module is the one place: ``Diagnostics`` is built from a
``Result`` once; ``to_dict()`` is what the protocol sends, ``caveats()`` the
sentences the atlas and the app show, ``issue_score()`` the number the
atlas is ordered by, and ``summary()`` the lines the command line prints.
The same numbers feed a terminal, a website and an app, and cannot drift.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .pipeline import Result


@dataclass(frozen=True)
class StopMatching:
    matched: int
    total: int
    by_station_id: int
    by_parent_station: int
    by_name: int
    # The first few stop ids that matched no node.
    unmatched: tuple[str, ...]

    @property
    def coverage(self) -> float:
        return self.matched / self.total if self.total else 0.0


@dataclass(frozen=True)
class Diagnostics:
    """A build's numbers. ``key``, ``name`` and ``date`` say what was built;
    the rest is what ``to_dict`` sends, in that order."""

    key: str
    name: str
    date: dt.date
    stations: int
    junctions: int
    edges: int
    lines: tuple[str, ...]
    octilinear: float
    stops: StopMatching
    trips_total: int
    paths: int
    unrouted: int
    skipped_calls: int
    borrowed_track: int
    labels_dropped: int
    peak_concurrent: int
    # How many stops the matcher could not place, in full: the dict carries
    # the first few, the score needs the count.
    unmatched_count: int = field(default=0)

    @classmethod
    def of(cls, result: Result) -> Diagnostics:
        from . import feeds
        from .render import octilinearity

        ok, total = octilinearity(result.graph)
        m, a = result.match, result.animation
        stations = len(result.graph.stations)
        return cls(
            key=result.key,
            name=feeds.get(result.key).name,
            date=result.date,
            stations=stations,
            junctions=len(result.graph.nodes) - stations,
            edges=len(result.graph.edges),
            lines=tuple(result.graph.labels),
            octilinear=ok / total if total else 0.0,
            stops=StopMatching(
                matched=len(m.stop_to_node),
                total=len(m.stop_to_node) + len(m.unmatched),
                by_station_id=m.by_id,
                by_parent_station=m.by_parent,
                by_name=m.by_name,
                unmatched=tuple(m.unmatched[:8]),
            ),
            trips_total=len(result.trips),
            paths=len(a.paths),
            unrouted=len(a.unrouted),
            skipped_calls=a.trips_with_skipped_calls,
            borrowed_track=a.trips_with_borrowed_track,
            labels_dropped=len(result.render.dropped_labels),
            peak_concurrent=result.peak_concurrent(),
            unmatched_count=len(m.unmatched),
        )

    def to_dict(self) -> dict[str, Any]:
        """The protocol's ``Diagnostics`` block, exactly as ``map.build`` has
        always sent it: what an app pins its types to."""
        return {
            "stations": self.stations,
            "junctions": self.junctions,
            "edges": self.edges,
            "lines": list(self.lines),
            "octilinear": self.octilinear,
            "stops": {"matched": self.stops.matched,
                      "total": self.stops.total,
                      "by": {"station_id": self.stops.by_station_id,
                             "parent_station": self.stops.by_parent_station,
                             "name": self.stops.by_name},
                      "unmatched": list(self.stops.unmatched)},
            "trips": {"total": self.trips_total, "paths": self.paths,
                      "unrouted": self.unrouted},
            "degraded": {"skipped_calls": self.skipped_calls,
                         "borrowed_track": self.borrowed_track},
            "labels_dropped": self.labels_dropped,
            "peak_concurrent": self.peak_concurrent,
        }

    def summary(self) -> str:
        """The lines the command line prints, unchanged from ``Result.summary()``."""
        s = self.stops
        report = (f"matched {s.matched}/{s.total} stops ({s.coverage:.0%}) "
                  f"[station_id={s.by_station_id}, parent_station={s.by_parent_station}, "
                  f"name={s.by_name}]"
                  + (f"; unmatched: {list(s.unmatched)}" if s.unmatched else ""))
        return "\n".join([
            f"{self.name} -- {self.date:%A %d %B %Y}",
            (f"  {self.stations + self.junctions} nodes ({self.stations} stations, "
             f"{self.junctions} junctions), {self.edges} edges, lines: {', '.join(self.lines)}"),
            f"  octilinear: {100 * self.octilinear:.1f}% of drawn length",
            f"  stops: {report}",
            f"  trips: {self.trips_total} routed onto {self.paths} distinct paths"
            + (f", {self.unrouted} UNROUTED" if self.unrouted else ""),
            (f"  degraded: {self.skipped_calls} trips skipped an unmatched stop, "
             f"{self.borrowed_track} borrowed another line's track"),
            f"  labels dropped: {self.labels_dropped}",
            f"  peak concurrent trains: {self.peak_concurrent}",
        ])


def caveats(diag: Diagnostics) -> list[str]:
    """What the pipeline had to fudge, phrased so a reader knows what it means.

    A bare count is alarming without being informative -- "6,627 trips" sounds
    catastrophic until you know it means a quarter of the hops are drawn one
    track over. Each line here says the consequence, not just the number.
    """
    out: list[str] = []
    trips = max(diag.trips_total, 1)
    if diag.unmatched_count:
        out.append(f"{diag.unmatched_count:,} of {diag.stops.total:,} stops could not be placed "
                   f"on the map, so trains pass straight through them")
    if diag.unrouted:
        out.append(f"{diag.unrouted:,} trips could not be traced across the "
                   f"network at all and are not shown")
    if diag.skipped_calls:
        pct = 100 * diag.skipped_calls / trips
        out.append(f"{diag.skipped_calls:,} trips ({pct:.0f}%) skip a "
                   f"stop the map does not carry")
    if diag.borrowed_track:
        pct = 100 * diag.borrowed_track / trips
        out.append(f"{diag.borrowed_track:,} trips ({pct:.0f}%) run part "
                   f"of the way on a neighbouring line's track, because the "
                   f"schematiser did not attribute that segment to their line. "
                   f"They follow the right corridor, but not always the right "
                   f"parallel track")
    if diag.labels_dropped:
        n = diag.labels_dropped
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


def issue_score(diag: Diagnostics) -> float:
    """How much of this network the pipeline had to fudge, as one number.

    Proportions, never counts: New York has more of everything, including
    stations, and ranking by raw totals would just re-sort the atlas by size.
    Zero means every stop placed, every trip traced on its own track, and every
    name drawn. Sorts the atlas, and is worth reading beside ``caveats``, which
    says the same things in words.
    """
    trips = max(diag.trips_total, 1)
    stops = max(diag.stops.total, 1)
    stations = max(diag.stations, 1)
    fractions = {
        "unplaced": diag.unmatched_count / stops,
        "unrouted": diag.unrouted / trips,
        "skipped": diag.skipped_calls / trips,
        "borrowed": diag.borrowed_track / trips,
        "unlabelled": diag.labels_dropped / stations,
    }
    return sum(ISSUE_WEIGHTS[k] * v for k, v in fractions.items())
