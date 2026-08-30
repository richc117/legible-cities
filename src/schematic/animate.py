"""Turn timed trips into an animation over the schematic map.

Each trip becomes a path through the drawn network plus a handful of keyframes
saying how far along that path the train is at a given second. The browser does
the interpolation: it looks up the two keyframes bracketing the current time,
lerps a distance, and asks the SVG path for the point at that distance. That
keeps the payload small and the motion smooth at any playback speed.

Two things make it cheap. Trips are routed over the *offset* track geometry, so
a train on a shared trunk rides its own line's track rather than the centreline.
And paths are deduplicated: a route's whole day collapses to a handful of
distinct stopping patterns, so a thousand trips reference a dozen paths.
"""

from __future__ import annotations

import datetime as dt
import heapq
import json
from html import escape as html_escape
from dataclasses import dataclass, field
from pathlib import Path

from . import linear
from .linegraph import LineGraph
from .names import display_name
from .offsets import cumulative_lengths, polyline_length
from .render import RenderResult, TrackPath
from .schedule import Trip

Coord = tuple[float, float]


class RoutingError(RuntimeError):
    pass


# Adjacency key for "any line", used when a trip runs over a segment the graph
# does not attribute to that trip's line.
ANY = "\x00any"


@dataclass
class RouteNetwork:
    """Per-line adjacency over the drawn tracks, for walking a trip's path."""

    adjacency: dict[str, dict[str, list[tuple[str, TrackPath]]]] = field(default_factory=dict)

    @classmethod
    def build(cls, render: RenderResult) -> RouteNetwork:
        adj: dict[str, dict[str, list[tuple[str, TrackPath]]]] = {}
        for (label, src, dst), tp in render.tracks.items():
            for key in (label, ANY):
                per_line = adj.setdefault(key, {})
                per_line.setdefault(src, []).append((dst, tp))
                per_line.setdefault(dst, []).append((src, tp))
        return cls(adjacency=adj)

    def shortest(self, label: str, start: str, goal: str) -> list[tuple[str, TrackPath]] | None:
        """Cheapest node sequence from ``start`` to ``goal`` along one line.

        ``topo`` can split what the timetable treats as one hop into several
        schematic edges, and can insert nodes the timetable never mentions, so
        consecutive stops are not always adjacent -- hence a search rather than
        a lookup.
        """
        graph = self.adjacency.get(label)
        if not graph or start not in graph or goal not in graph:
            return None
        if start == goal:
            return []

        dist: dict[str, float] = {start: 0.0}
        prev: dict[str, tuple[str, TrackPath]] = {}
        queue: list[tuple[float, str]] = [(0.0, start)]
        seen: set[str] = set()
        while queue:
            d, node = heapq.heappop(queue)
            if node in seen:
                continue
            seen.add(node)
            if node == goal:
                break
            for nxt, tp in graph.get(node, ()):
                nd = d + tp.length
                if nd < dist.get(nxt, float("inf")):
                    dist[nxt] = nd
                    prev[nxt] = (node, tp)
                    heapq.heappush(queue, (nd, nxt))

        if goal not in prev:
            return None
        out: list[tuple[str, TrackPath]] = []
        cur = goal
        while cur != start:
            back, tp = prev[cur]
            out.append((cur, tp))
            cur = back
        out.reverse()
        return out


def _oriented(tp: TrackPath, from_node: str) -> list[Coord]:
    """The track's points running away from ``from_node``."""
    return list(tp.points) if tp.src == from_node else list(reversed(tp.points))


@dataclass
class TripPath:
    """A trip's geometry through the map, with distance marked at each stop."""

    points: list[Coord]
    stop_lengths: list[float]
    # Hops that had to be routed over another line's track because the graph
    # does not carry this line there. The train still moves; it just rides a
    # neighbouring track for that segment.
    borrowed_hops: int = 0

    def path_d(self, precision: int = 1) -> str:
        p = self.points
        return (f"M{p[0][0]:.{precision}f} {p[0][1]:.{precision}f}"
                + "".join(f"L{x:.{precision}f} {y:.{precision}f}" for x, y in p[1:]))


def build_trip_path(net: RouteNetwork, nodes: list[str], label: str) -> TripPath | None:
    """Walk a node sequence across the drawn network. None if it cannot be routed."""
    if not nodes or any(n is None for n in nodes):
        return None

    points: list[Coord] = []
    stop_lengths: list[float] = []
    run = 0.0
    borrowed = 0

    for i, node in enumerate(nodes):
        if i == 0:
            continue
        hop = net.shortest(label, nodes[i - 1], node)
        if hop is None:
            # LOOM does not always attribute every segment a timetable uses to
            # the line using it (BART's Fruitvale-Coliseum, for one). Riding a
            # neighbouring track there beats dropping the train for the day.
            hop = net.shortest(ANY, nodes[i - 1], node)
            if hop is None:
                return None
            borrowed += 1
        cursor = nodes[i - 1]
        if not points:
            first = _oriented(hop[0][1], cursor) if hop else None
            points.append(first[0] if first else (0.0, 0.0))
            stop_lengths.append(0.0)
        for nxt, tp in hop:
            seg = _oriented(tp, cursor)
            # The previous segment already ended on this point.
            run += polyline_length(seg)
            points.extend(seg[1:])
            cursor = nxt
        stop_lengths.append(run)

    if len(points) < 2 or run <= 0:
        return None
    return TripPath(points=points, stop_lengths=stop_lengths, borrowed_hops=borrowed)


@dataclass
class Animation:
    date: dt.date
    paths: list[dict]
    trips: list[dict]
    lines: dict[str, str]
    unrouted: list[str]
    # Column assignments for the linear view; rows are placed in the browser,
    # since their order depends on the sort the reader picks.
    linear: dict = field(default_factory=dict)
    # Trips that lost at least one call because its stop matched no node.
    trips_with_skipped_calls: int = 0
    # Trips routed over another line's track for at least one hop.
    trips_with_borrowed_track: int = 0

    def to_json(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "lines": self.lines,
            "linear": self.linear,
            "paths": self.paths,
            "trips": self.trips,
        }


def build(render: RenderResult, graph: LineGraph, trips: list[Trip],
          date: dt.date, line_order: list[str] | None = None) -> Animation:
    """Route every trip and collect the deduplicated paths."""
    net = RouteNetwork.build(render)
    layout = linear.build(graph, order=line_order)
    layout_json = layout.to_json()
    # Every station in the layout gets its name here rather than being read off
    # the drawn map, whose label placer drops names it cannot fit.
    layout_json["names"] = {
        n.id: display_name(n.station_label)
        for n in graph.stations if n.station_label
    }

    colors: dict[str, str] = {}
    for e in graph.edges:
        for ln in e.lines:
            if ln.color:
                colors.setdefault(ln.label, ln.color if ln.color.startswith("#") else f"#{ln.color}")

    path_index: dict[tuple, int] = {}
    paths: list[dict] = []
    out_trips: list[dict] = []
    unrouted: list[str] = []

    skipped_calls = 0
    borrowed = 0

    for trip in trips:
        # A stop that matched no node would leave a hole in the path. Drop just
        # that call -- the train glides past it -- rather than the whole trip,
        # which in a combined feed can be hundreds of services.
        calls = [c for c in trip.calls if c.node_id is not None]
        if len(calls) < len(trip.calls):
            skipped_calls += 1
        if len(calls) < 2:
            unrouted.append(trip.trip_id)
            continue

        nodes = [c.node_id for c in calls]
        key = (trip.route_label, tuple(nodes))
        idx = path_index.get(key)
        if idx is None:
            tp = build_trip_path(net, nodes, trip.route_label)
            if tp is None:
                unrouted.append(trip.trip_id)
                continue
            idx = len(paths)
            path_index[key] = idx
            paths.append({"route": trip.route_label, "d": tp.path_d(),
                          "stops": [round(v, 1) for v in tp.stop_lengths],
                          # The station behind each stop, so a view that is not
                          # the map can place the train from the layout instead
                          # of from arc length.
                          "nodes": list(nodes),
                          "borrowed": tp.borrowed_hops})
        if paths[idx]["borrowed"]:
            borrowed += 1
        stops = paths[idx]["stops"]
        if len(stops) != len(calls):
            unrouted.append(trip.trip_id)
            continue

        # Keyframes: arrive at a stop, then hold there until departure. The dwell
        # pair is what stops trains gliding through stations without pausing.
        #
        # The value is the *stop index*, not a distance. Every view then derives
        # its own geometry from the same fractional index -- arc length along
        # the drawn path for the map, a position on a row for the linear view --
        # and the two stay in step by construction. It is also much smaller on
        # the wire than a float length, which matters at NYC's 257,000
        # keyframes.
        keys: list[list[float]] = []
        for i, call in enumerate(calls):
            keys.append([call.arrival, i])
            if call.departure > call.arrival:
                keys.append([call.departure, i])
        # LA publishes an empty trip_headsign for all 8,561 of its trips, so
        # "where is this train going" -- the one thing a viewer actually wants
        # from a moving dot -- has to fall back to naming its last stop.
        where = trip.headsign.strip()
        if not where:
            last = graph.nodes.get(calls[-1].node_id)
            if last and last.station_label:
                where = display_name(last.station_label)
        out_trips.append({"p": idx, "r": trip.route_label, "h": where, "k": keys})

    out_trips.sort(key=lambda t: t["k"][0][0])
    return Animation(date=date, paths=paths, trips=out_trips, lines=colors,
                     unrouted=unrouted, trips_with_skipped_calls=skipped_calls,
                     trips_with_borrowed_track=borrowed,
                     linear=layout_json)


# Emitted only when the caller says where the icons live, so a page written for
# standalone use does not link assets that are not beside it.
_ICON_LINKS = """<link rel="icon" href="{base}/favicon.svg" type="image/svg+xml">
<link rel="icon" href="{base}/favicon-96x96.png" type="image/png" sizes="96x96">
<link rel="apple-touch-icon" href="{base}/apple-touch-icon.png">"""


def write(animation: Animation, svg: str, out_dir: Path, *,
          stem: str = "animation", title: str = "Transit animation",
          name: str | None = None, subtitle: str = "",
          back: str = "index.html", icons: str | None = None) -> tuple[Path, Path]:
    """Write ``<stem>.positions.json`` and a self-contained ``<stem>.html``.

    The stem is per-city: with a fixed filename, generating a second network
    silently overwrites the first one's animation.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    data = animation.to_json()

    json_path = out_dir / f"{stem}.positions.json"
    json_path.write_text(json.dumps(data, separators=(",", ":")))

    html_path = out_dir / f"{stem}.html"
    html_path.write_text(
        _HTML.replace("__PRESENT__", _PRESENT_JS)
             .replace("__ICONS__", _ICON_LINKS.format(base=icons) if icons else "")
             .replace("__TITLE__", html_escape(title))
             .replace("__NAME__", html_escape(name or title))
             .replace("__SUBTITLE__", html_escape(subtitle))
             .replace("__BACK__", html_escape(back))
             .replace("__SVG__", svg)
             .replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    return json_path, html_path


# The page lives as a real HTML file rather than a string literal. It is 950
# lines of HTML, CSS and JavaScript, and keeping it inside Python costs syntax
# highlighting, linting, and any diff worth reading.
#
# present.js is inlined into it rather than shipped alongside, because the page
# has to stay a single self-contained file: it is opened from file:// during an
# export, and served from a subpath on the site.
_PAGE_DIR = Path(__file__).parent / "page"
_HTML = (_PAGE_DIR / "page.html").read_text()
_PRESENT_JS = (_PAGE_DIR / "present.js").read_text()
