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
        _HTML.replace("__ICONS__", _ICON_LINKS.format(base=icons) if icons else "")
             .replace("__TITLE__", html_escape(title))
             .replace("__NAME__", html_escape(name or title))
             .replace("__SUBTITLE__", html_escape(subtitle))
             .replace("__BACK__", html_escape(back))
             .replace("__SVG__", svg)
             .replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    return json_path, html_path


_HTML = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
__ICONS__
<script>
  // Applied before paint so the page never flashes the wrong theme. Shares the
  // rc-theme key with the rest of the site.
  (function () {
    var stored = null;
    try { stored = localStorage.getItem("rc-theme"); } catch (e) {}
    if ((stored || "warm-dark") === "sepia") {
      document.documentElement.setAttribute("data-theme", "sepia");
    }
  })();
</script>
<style>
  :root {
    --bg: #15120f; --bg-soft: #1f1915; --text: #f2ede6; --muted: #c3b8aa;
    --border: #3a2f27; --link-hover: #ded5c8; --focus: #81a5ff;
    /* Consumed by the inlined SVG. Only page furniture -- never a line colour,
       which belongs to the agency and is drawn literally. */
    --map-bg: #1f1915; --map-station-fill: #f2ede6;
    --map-station-stroke: #15120f; --map-label: #f2ede6;
    --train-halo: #15120f;
  }
  :root[data-theme="sepia"] {
    --bg: #f7efe1; --bg-soft: #f0e2cf; --text: #2d241d; --muted: #655748;
    --border: #cab9a2; --link-hover: #1f1812; --focus: #4068cf;
    --map-bg: #f7efe1; --map-station-fill: #f7efe1;
    --map-station-stroke: #2d241d; --map-label: #2d241d;
    --train-halo: #f7efe1;
  }
  *, *::before, *::after { box-sizing: border-box; }
  html, body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
    font-size: 18px; line-height: 1.72;
  }
  a { color: var(--text); text-decoration-thickness: 1px; text-underline-offset: 2px; }
  a:hover { color: var(--link-hover); }
  a:focus-visible, button:focus-visible, input:focus-visible {
    outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 2px;
  }
  header {
    background: var(--bg); border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 5; padding: 0.7rem 1.4rem 0.6rem;
  }
  .bar {
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.55rem 1.1rem;
  }
  .bar + .bar { margin-top: 0.5rem; }
  .spacer { flex: 1 1 auto; }
  h1 {
    font-size: 1.05rem; font-weight: 700; margin: 0;
    min-width: 0; flex: 0 1 auto;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .back { font-size: 0.86rem; color: var(--muted); text-decoration: none;
          white-space: nowrap; }
  .back:hover { color: var(--text); }
  .clock { font-variant-numeric: tabular-nums; font-size: 1.35rem; min-width: 5.5ch; }
  .sub { color: var(--muted); font-size: 0.86rem; }
  #count::after { content: " trains"; }
  .group { display: flex; align-items: center; gap: 0.6rem; }
  button {
    font: inherit; font-size: 0.86rem; color: var(--muted); background: none;
    border: 0; border-bottom: 1px solid transparent; padding: 0.1rem 0;
    cursor: pointer; white-space: nowrap;
  }
  button:hover { color: var(--text); }
  button[aria-pressed="true"] { color: var(--text); border-bottom-color: var(--text); }
  /* Play/pause says which it is in its own label; the pressed underline just
     reads as a stray rule under the word. */
  #play[aria-pressed="true"] { border-bottom-color: transparent; }

  /* An exclusive choice should not look like the toggles beside it. Sharing a
     ground and a border makes "one of these" read at a glance, which the bare
     words did not. */
  .segmented {
    display: inline-flex; border: 1px solid var(--border); border-radius: 999px;
    overflow: hidden; background: var(--bg-soft);
  }
  .segmented button {
    border: 0; border-radius: 0; padding: 0.28rem 0.85rem; color: var(--muted);
  }
  .segmented button + button { border-left: 1px solid var(--border); }
  .segmented button[aria-pressed="true"] {
    background: var(--text); color: var(--bg); border-bottom-color: transparent;
  }

  input[type=range] {
    flex: 1 1 12rem; min-width: 8rem; max-width: 26rem; accent-color: var(--text);
  }

  /* Secondary controls: everything you touch once a session rather than once a
     minute. Open by default where there is room, folded away on a phone. */
  #more-panel { margin-top: 0.55rem; }
  #more-panel[hidden] { display: none; }
  .panel-row {
    display: flex; flex-wrap: wrap; align-items: center; gap: 0.45rem 1.4rem;
    padding: 0.4rem 0 0.2rem;
  }
  /* Clusters stay together when the row wraps, so "All / None" never ends up
     orphaned from the word Lines. */
  .panel-group {
    display: inline-flex; align-items: center; gap: 0.6rem;
  }
  .panel-label { color: var(--muted); font-size: 0.86rem; }
  .lines { display: flex; gap: 0.7rem; flex-wrap: wrap; }
  .chip {
    display: inline-flex; align-items: center; gap: 0.35rem; cursor: pointer;
    user-select: none; font-size: 0.86rem; color: var(--muted);
  }
  .chip:hover { color: var(--text); }
  .chip .dot { width: 9px; height: 9px; border-radius: 50%; }
  .chip[aria-pressed="true"] { color: var(--text); }
  .chip[aria-pressed="false"] .dot { opacity: 0.3; }
  .sort { color: var(--muted); font-size: 0.86rem; display: inline-flex;
          align-items: center; gap: 0.45rem; }
  .sort[hidden] { display: none; }

  /* The stage scrolls, not the page. The inset edge shading is the only hint
     that there is more chart to the right, so it is worth the two lines. */
  #stage {
    padding: 1.4rem; overflow-x: auto; -webkit-overflow-scrolling: touch;
    overscroll-behavior-x: contain;
  }
  #stage.scrollable-right {
    -webkit-mask-image: linear-gradient(to right, #000 92%, rgba(0,0,0,0.35));
            mask-image: linear-gradient(to right, #000 92%, rgba(0,0,0,0.35));
  }
  /* Row labels in the linear view's gutter, in each line's own colour. */
  .rowname { font-weight: 700; font-size: 13px; dominant-baseline: middle; }
  /* Width is set by the script, which is the only thing that knows the view:
     the map wants to fill the height and pan, while the rows want a legible
     column width and let the page scroll. A CSS rule cannot tell them apart,
     and the one that used to try pinned the width to 100% and killed the
     panning it was meant to enable. */
  svg { display: block; height: auto; }
  /* Painted behind the glyph so a train reads against its own line colour. */
  .train { paint-order: stroke; stroke: var(--train-halo); stroke-width: 1.6; }
  @media (max-width: 700px) {
    html, body { font-size: 17px; }
    /* Two rows, not four. The toolbar was taking a third of the screen before
       anything of the map appeared. */
    header { padding: 0.35rem 0.7rem; }
    /* nowrap, deliberately: a wrapping flex line wraps *before* it shrinks, so
       with wrap on the title never truncates and the view switcher drops to a
       row of its own. Everything here can shrink -- the title to an ellipsis,
       the scrubber to a stub -- so one line is always reachable. */
    .bar { gap: 0.2rem 0.7rem; flex-wrap: nowrap; }
    .bar + .bar { margin-top: 0.1rem; }
    h1 { font-size: 0.95rem; }
    .clock { font-size: 1rem; min-width: 4.6ch; }
    /* The service date is on the atlas entry that got you here. */
    .sub:not(#count) { display: none; }
    #count { font-size: 0.78rem; }
    #count::after { content: none; }
    input[type=range] { min-width: 3.5rem; flex: 1 1 3.5rem; }
    .spacer { display: none; }
    .back { font-size: 0; }              /* keeps the arrow, drops the word */
    .back::before { content: "\2190"; font-size: 1rem; }
    .segmented button { padding: 0 0.6rem; font-size: 0.8rem; }
    button { font-size: 0.8rem; }
    #stage { padding: 0.7rem 0; }
  }

  /* Both platforms ask for 44px. Scoped to touch so the desktop toolbar keeps
     its density. */
  @media (pointer: coarse) {
    button, .chip, .back {
      min-height: 44px; display: inline-flex; align-items: center;
    }
    .segmented button { padding: 0 0.95rem; }
    input[type=range] { height: 44px; }
  }
</style>
<header>
  <div class="bar">
    <a class="back" href="__BACK__">&larr; Atlas</a>
    <h1>__NAME__</h1>
    <span class="sub">__SUBTITLE__</span>
    <span class="spacer"></span>
    <span class="segmented" role="group" aria-label="View">
      <button id="view-map" aria-pressed="true">Map</button>
      <button id="view-linear" aria-pressed="false">Linear</button>
      <button id="view-string" aria-pressed="false">Time</button>
    </span>
  </div>
  <div class="bar">
    <button id="play" aria-pressed="true">Pause</button>
    <div class="clock" id="clock">--:--</div>
    <input type="range" id="scrub" min="0" max="1" step="1" value="0"
           aria-label="Time of day">
    <span class="sub" id="count">0 trains</span>
    <button id="speed" aria-label="Playback speed">60&times;</button>
    <button id="more" aria-expanded="false" aria-controls="more-panel">More &#9662;</button>
  </div>
  <div id="more-panel" hidden>
    <div class="panel-row">
      <span class="panel-group">
        <span class="panel-label">Lines <span id="line-count"></span></span>
        <button id="lines-all">All</button>
        <button id="lines-none">None</button>
      </span>
      <span class="lines" id="line-toggles"></span>
      <span class="panel-group">
        <button id="labels-toggle" aria-pressed="true">Labels</button>
        <span class="sort" id="sort-group" hidden>
          <span class="panel-label">Sort</span>
          <button id="sort-line" aria-pressed="true">A&ndash;Z</button>
          <button id="sort-size" aria-pressed="false">Stations</button>
        </span>
      </span>
    </div>
  </div>
</header>
<main id="stage">__SVG__</main>
<script id="data" type="application/json">__DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById("data").textContent);
  const svg = document.querySelector("#stage svg");
  const NS = "http://www.w3.org/2000/svg";
  const reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

  // ---------------------------------------------------------------- geometry
  const vb = svg.getAttribute("viewBox").split(/\s+/).map(Number);
  const [vbX, vbY, vbW, vbH] = vb;
  // The generated SVG carries width/height attributes, which fix its intrinsic
  // aspect ratio -- so  would ignore a viewBox that grows for the
  // linear view. CSS sizes the element here, so drop them.
  svg.removeAttribute("width");
  svg.removeAttribute("height");

  // Where each station sits on the map, read back off the drawing rather than
  // shipped a second time in the payload.
  const mapXY = new Map();
  svg.querySelectorAll("#stations circle[data-node]").forEach(c => {
    mapXY.set(c.getAttribute("data-node"), {
      x: +c.getAttribute("cx"), y: +c.getAttribute("cy"), r: +c.getAttribute("r"),
    });
  });

  // The linear layout: one row per line, plus a shallower row per branch.
  const layout = data.linear || { columns: 1, lines: [] };
  // Station names sit above their dot at an angle, so a row needs clear space
  // above it and past its right-hand end or the outermost names get cut off.
  const LABEL_RISE = 112;
  // A row needs this much height for its names to be readable. A network with
  // many lines therefore needs a taller canvas than the map does -- so the
  // linear view grows the viewBox and the page scrolls, the way a list should,
  // rather than crushing 27 rows into the map's frame.
  const ROW_H = 150;
  const nLines = Math.max(layout.lines.length, 1);
  const linH = Math.max(vbH, LABEL_RISE + nLines * ROW_H);
  // The gutter has to hold the widest terminus name the time chart will print
  // there, or the left-hand end of it is sliced off by the viewBox.
  const terminusWidth = (() => {
    let longest = 0;
    for (const line of layout.lines) {
      const spine = (line.rows[0] || {}).nodes || [];
      for (const pair of [spine[0], spine[spine.length - 1]]) {
        const nm = pair && (layout.names || {})[pair[0]];
        if (nm) longest = Math.max(longest, nm.length);
      }
    }
    return longest * 7.0 + 24;      // 11px Palatino runs wide on capitals
  })();
  const GUTTER = Math.max(vbW * 0.075, 74, terminusWidth);
  const RIGHT = Math.max(vbW * 0.02, 96);
  const colW = (vbW - GUTTER - RIGHT) / Math.max(layout.columns - 1, 1);
  const band = (linH - LABEL_RISE) / nLines;
  const branchDrop = Math.min(band * 0.30, 26);

  // Column of every (line, station), and which row of the line it is on.
  const cell = new Map();                       // "label node" -> {col, depth}
  const lineStations = new Map();               // label -> [{node, col, depth}]
  for (const line of layout.lines) {
    const list = [];
    for (const row of line.rows) {
      for (const pair of row.nodes) {
        cell.set(line.label + " " + pair[0], { col: pair[1], depth: row.depth });
        list.push({ node: pair[0], col: pair[1], depth: row.depth });
      }
    }
    lineStations.set(line.label, list);
  }

  // Row order is animated, so a re-sort slides rather than jumps.
  const order = layout.lines.map(l => l.label);
  const rowPos = new Map(order.map((l, i) => [l, i]));
  const rowTarget = new Map(rowPos);

  const linXY = (label, node) => {
    const c = cell.get(label + " " + node);
    if (!c) return null;
    const base = rowPos.get(label) || 0;
    return {
      x: vbX + GUTTER + c.col * colW,
      y: vbY + LABEL_RISE + band * (base + 0.15) + c.depth * branchDrop,
    };
  };

  // A station column narrower than this cannot be read, so on a narrow screen
  // the rows keep their width and the stage scrolls instead of shrinking.
  const MIN_COL_PX = 22;
  const stage = document.querySelector("#stage");

  function sizeStage() {
    const cs = getComputedStyle(stage);
    const avail = stage.clientWidth
      - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    const box = svg.getAttribute("viewBox").split(/\s+/).map(Number);
    const narrow = window.innerWidth <= 900;
    let w = avail;
    if (narrow && mode === "map") {
      // A wide network shrunk to a phone is unreadable: fill the height and
      // let it pan.
      w = Math.max(avail, window.innerHeight * 0.62 * (box[2] / box[3]));
    } else if (narrow) {
      // Rows keep a legible column width; the page takes the vertical scroll.
      w = Math.max(avail, layout.columns * MIN_COL_PX);
    }
    svg.style.width = w.toFixed(0) + "px";
    svg.style.height = "auto";
    stage.classList.toggle("scrollable-right",
                           stage.scrollWidth > stage.clientWidth + 1);
  }

  const lerp = (a, b, t) => a + (b - a) * t;
  const ease = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  // ------------------------------------------------------- stringline layout
  // A Marey chart: time across, stops down, every trip a diagonal. It reuses
  // the linear layout's columns as its vertical axis, so a station is at the
  // same position in the sequence in both views -- the row simply stands up.
  const lineWidth = new Map(layout.lines.map(l => [l.label, Math.max(l.stations, 2)]));
  const lineSpan = new Map(layout.lines.map(l => {
    let max = 1;
    for (const row of l.rows) for (const pair of row.nodes) max = Math.max(max, pair[1]);
    return [l.label, max];
  }));

  const bandTop = label => vbY + LABEL_RISE + band * (rowPos.get(label) || 0) + 4;
  const bandInner = () => band * 0.80;

  // Vertical position of a station within its line's band, band-local.
  const stopY = (label, node) => {
    const c = cell.get(label + " " + node);
    if (!c) return null;
    return (c.col / (lineSpan.get(label) || 1)) * bandInner();
  };

  // ------------------------------------------------------------ linear layer
  // An interchange is one dot on the map but appears once per line in the
  // linear view, so stations and labels are rebuilt per (line, station). At
  // m = 0 every instance sits exactly where the map drew it, which makes the
  // swap invisible.
  const backdrop = svg.querySelector("#backdrop");
  const mapStations = svg.querySelector("#stations");
  const mapLabels = svg.querySelector("#labels");
  const labelSpec = new Map();
  if (mapLabels) {
    mapLabels.querySelectorAll("text[data-node]").forEach(t => {
      labelSpec.set(t.getAttribute("data-node"), {
        text: t.textContent,
        x: +t.getAttribute("x"), y: +t.getAttribute("y"),
        anchor: t.getAttribute("text-anchor") || "start",
        rotate: (t.getAttribute("transform") || "").match(/rotate\(([-\d.]+)/),
      });
    });
  }

  const dotLayer = document.createElementNS(NS, "g");
  const nameLayer = document.createElementNS(NS, "g");
  const textLayer = document.createElementNS(NS, "g");
  dotLayer.setAttribute("id", "linear-stations");
  textLayer.setAttribute("id", "linear-labels");
  nameLayer.setAttribute("id", "linear-names");
  if (mapLabels) {
    textLayer.setAttribute("font-size", mapLabels.getAttribute("font-size") || "11");
    textLayer.setAttribute("fill", mapLabels.getAttribute("fill") || "currentColor");
  }

  const dots = [];      // {el, label, node, mapPt, r}
  const texts = [];     // {el, label, node, spec, primary}
  const names = [];     // {el, label}
  const seenLabel = new Set();

  for (const line of layout.lines) {
    for (const st of lineStations.get(line.label) || []) {
      const pt = mapXY.get(st.node);
      if (!pt) continue;

      const dot = document.createElementNS(NS, "circle");
      dot.setAttribute("r", pt.r);
      dot.setAttribute("fill", "var(--map-station-fill, #fff)");
      dot.setAttribute("stroke", "var(--map-station-stroke, #111)");
      dot.setAttribute("stroke-width", "2.2");
      dotLayer.appendChild(dot);
      dots.push({ el: dot, label: line.label, node: st.node, mapPt: pt, r: pt.r });

      const name = (layout.names || {})[st.node];
      if (name) {
        const t = document.createElementNS(NS, "text");
        t.textContent = name;
        textLayer.appendChild(t);
        // The map may have had nowhere to put this name. Then there is no
        // m = 0 pose to hold, so park it on the dot and keep it invisible
        // until the rows take over.
        const spec = labelSpec.get(st.node) ||
          { x: pt.x, y: pt.y, anchor: "start", rotate: null, ghost: true };
        // Only one instance per station is opaque on the map, or interchange
        // names would overprint themselves.
        const primary = !spec.ghost && !seenLabel.has(st.node);
        seenLabel.add(st.node);
        texts.push({ el: t, label: line.label, node: st.node, spec: spec, primary: primary });
      }
    }
    const name = document.createElementNS(NS, "text");
    name.setAttribute("class", "rowname");
    name.setAttribute("text-anchor", "end");
    name.setAttribute("fill", data.lines[line.label] || "currentColor");
    name.textContent = line.label;
    nameLayer.appendChild(name);
    names.push({ el: name, label: line.label });
  }

  if (mapStations) mapStations.style.display = "none";
  if (mapLabels) mapLabels.style.display = "none";
  const trainsGroup = svg.querySelector("#trains");
  svg.insertBefore(nameLayer, trainsGroup);
  svg.insertBefore(dotLayer, trainsGroup);
  svg.insertBefore(textLayer, trainsGroup);

  // ------------------------------------------------------------- stringline
  // Each route's trips are concatenated into one path in band-local space, so
  // re-sorting only moves a group transform rather than rebuilding 8,000
  // subpaths. Drawn faintly: where the diagonals crowd, that is frequency.
  const stringLayer = document.createElementNS(NS, "g");
  stringLayer.setAttribute("id", "stringline");
  stringLayer.setAttribute("opacity", "0");
  const stringGroups = new Map();
  const axisLayer = document.createElementNS(NS, "g");
  axisLayer.setAttribute("id", "stringline-axis");

  // ------------------------------------------------------------ track morph
  const tracks = [];
  svg.querySelectorAll("#lines path[data-src]").forEach(el => {
    const label = el.parentNode.getAttribute("data-line");
    const pts = el.getAttribute("d").trim().split(/[ML]\s*/).filter(Boolean)
      .map(p => {
        const xy = p.trim().split(/\s+/).map(Number);
        return { x: xy[0], y: xy[1] };
      });
    tracks.push({
      el: el, label: label, pts: pts, d0: el.getAttribute("d"),
      src: el.getAttribute("data-src"), dst: el.getAttribute("data-dst"),
    });
  });

  const trackD = (t, m) => {
    const a = linXY(t.label, t.src), b = linXY(t.label, t.dst);
    if (!a || !b) return t.d0;
    const n = t.pts.length - 1;
    let out = "";
    for (let i = 0; i <= n; i++) {
      const f = n === 0 ? 0 : i / n;
      const lx = lerp(a.x, b.x, f), ly = lerp(a.y, b.y, f);
      const x = lerp(t.pts[i].x, lx, m), y = lerp(t.pts[i].y, ly, m);
      out += (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }
    return out;
  };

  // ------------------------------------------------------------------ trains
  const defs = document.createElementNS(NS, "defs");
  const pathEls = data.paths.map(p => {
    const el = document.createElementNS(NS, "path");
    el.setAttribute("d", p.d);
    defs.appendChild(el);
    return el;
  });
  svg.appendChild(defs);
  const layer = trainsGroup;

  const trips = data.trips;
  const starts = trips.map(t => t.k[0][0]);
  const ends = trips.map(t => t.k[t.k.length - 1][0]);
  const maxSpan = Math.max.apply(null, trips.map((t, i) => ends[i] - starts[i]));
  const t0 = Math.min.apply(null, starts), t1 = Math.max.apply(null, ends);

  const routes = [...new Set(trips.map(t => t.r))].sort();
  const hidden = new Set();
  const nodes = new Map();

  let now = 7 * 3600, speed = 60, playing = true, last = performance.now();
  // Two tweens rather than one three-way switch: m unfolds the map into rows,
  // s stands those rows up into a time chart. A jump from map to stringline
  // simply runs both at once.
  let view = 0, viewTarget = 0;     // m: 0 = map, 1 = linear rows
  let str = 0, strTarget = 0;       // s: 0 = network, 1 = stringline
  let mode = "map";                 // map | linear | string
  let dirty = true;

  const clock = document.getElementById("clock");
  const count = document.getElementById("count");
  const scrub = document.getElementById("scrub");
  scrub.min = t0; scrub.max = t1; scrub.value = now;

  const timeX = t =>
    vbX + GUTTER + ((t - t0) / Math.max(t1 - t0, 1)) * (vbW - GUTTER - RIGHT);

  (function buildStringline() {
    const byRoute = new Map();
    for (const trip of trips) {
      const path = data.paths[trip.p];
      let d = "";
      for (let i = 0; i < trip.k.length; i++) {
        const f = trip.k[i][1];
        const lo = Math.min(Math.floor(f), path.nodes.length - 1);
        const hi = Math.min(lo + 1, path.nodes.length - 1);
        const ya = stopY(trip.r, path.nodes[lo]), yb = stopY(trip.r, path.nodes[hi]);
        if (ya === null || yb === null) continue;
        const y = lerp(ya, yb, f - lo);
        d += (d ? "L" : "M") + timeX(trip.k[i][0]).toFixed(1) + " " + y.toFixed(1);
      }
      if (d.indexOf("L") < 0) continue;   // a single point draws nothing
      if (!byRoute.has(trip.r)) byRoute.set(trip.r, []);
      byRoute.get(trip.r).push(d);
    }
    for (const line of layout.lines) {
      const g = document.createElementNS(NS, "g");
      const el = document.createElementNS(NS, "path");
      el.setAttribute("d", (byRoute.get(line.label) || []).join(""));
      el.setAttribute("fill", "none");
      el.setAttribute("stroke", data.lines[line.label] || "#888");
      el.setAttribute("stroke-width", "1.4");
      el.setAttribute("stroke-linecap", "round");
      // Busy lines would otherwise fill in solid; thinning them lets density
      // read as density rather than as a block of colour.
      const busy = (byRoute.get(line.label) || []).length;
      el.setAttribute("opacity", (busy > 400 ? 0.16 : busy > 180 ? 0.28
                                 : busy > 90 ? 0.4 : 0.55).toFixed(2));
      g.appendChild(el);

      // Without the two termini the vertical axis has no reference: you can see
      // that a train is climbing but not what it is climbing towards.
      const spine = line.rows[0] ? line.rows[0].nodes : [];
      if (spine.length > 1) {
        const ends = [spine[0], spine[spine.length - 1]];
        ends.forEach((pair, i) => {
          const nm = (layout.names || {})[pair[0]];
          if (!nm) return;
          const t = document.createElementNS(NS, "text");
          t.setAttribute("x", (vbX + GUTTER - 14).toFixed(1));
          t.setAttribute("y", (i === 0 ? 4 : bandInner() + 4).toFixed(1));
          t.setAttribute("text-anchor", "end");
          t.setAttribute("font-size", "11");
          t.setAttribute("fill", "var(--map-label, #111)");
          t.setAttribute("opacity", "0.6");
          t.textContent = nm;
          g.appendChild(t);
        });
      }

      stringLayer.appendChild(g);
      stringGroups.set(line.label, g);
    }
    // Hour gridlines, so the time axis can actually be read.
    const step = (t1 - t0) > 12 * 3600 ? 3 * 3600 : 3600;
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      const x = timeX(t);
      const tick = document.createElementNS(NS, "line");
      tick.setAttribute("x1", x.toFixed(1)); tick.setAttribute("x2", x.toFixed(1));
      tick.setAttribute("y1", (vbY + LABEL_RISE - 26).toFixed(1));
      tick.setAttribute("y2", (vbY + LABEL_RISE + band * nLines).toFixed(1));
      tick.setAttribute("stroke", "var(--map-label, #111)");
      tick.setAttribute("stroke-width", "0.6");
      tick.setAttribute("opacity", "0.16");
      axisLayer.appendChild(tick);
      const lab = document.createElementNS(NS, "text");
      lab.setAttribute("x", x.toFixed(1));
      lab.setAttribute("y", (vbY + LABEL_RISE - 32).toFixed(1));
      lab.setAttribute("text-anchor", "middle");
      lab.setAttribute("font-size", "13");
      lab.setAttribute("fill", "var(--map-label, #111)");
      lab.setAttribute("opacity", "0.55");
      lab.textContent = String(Math.floor(t / 3600) % 24).padStart(2, "0") + ":00";
      axisLayer.appendChild(lab);
    }
    stringLayer.appendChild(axisLayer);
    // The playhead ties the chart to the clock: it is the same instant the
    // network views are showing.
    const head = document.createElementNS(NS, "line");
    head.setAttribute("id", "playhead");
    head.setAttribute("stroke", "var(--map-label, #111)");
    head.setAttribute("stroke-width", "1.2");
    head.setAttribute("opacity", "0.75");
    stringLayer.appendChild(head);
    svg.insertBefore(stringLayer, trainsGroup);
  })();
  const playhead = svg.querySelector("#playhead");

  const fmt = s => {
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return String(h % 24).padStart(2, "0") + ":" + String(m).padStart(2, "0")
         + (h >= 24 ? " +1d" : "");
  };

  // Fractional stop index at time t. Both views read this same number, which
  // is what keeps them in step.
  const stopAt = (keys, t) => {
    let lo = 0, hi = keys.length - 1;
    if (t <= keys[0][0]) return keys[0][1];
    if (t >= keys[hi][0]) return keys[hi][1];
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (keys[mid][0] <= t) lo = mid; else hi = mid;
    }
    const ka = keys[lo], kb = keys[hi];
    return kb[0] === ka[0] ? kb[1] : ka[1] + (kb[1] - ka[1]) * (t - ka[0]) / (kb[0] - ka[0]);
  };

  const lowerBound = t => {
    let lo = 0, hi = starts.length;
    const cutoff = t - maxSpan;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (starts[mid] < cutoff) lo = mid + 1; else hi = mid;
    }
    return lo;
  };

  function geometry(m, sf) {
    // Network layers dim as the chart takes over; both are positioned every
    // frame so a re-sort moves them together.
    const networkOpacity = (1 - sf).toFixed(3);
    for (const g of [dotLayer, textLayer, svg.querySelector("#lines")]) {
      if (g) g.setAttribute("opacity", networkOpacity);
    }
    stringLayer.setAttribute("opacity", sf.toFixed(3));
    for (const line of layout.lines) {
      const g = stringGroups.get(line.label);
      if (g) g.setAttribute("transform", "translate(0," + bandTop(line.label).toFixed(1) + ")");
    }
    if (linH !== vbH) {
      const h = lerp(vbH, linH, m);
      svg.setAttribute("viewBox", vbX + " " + vbY + " " + vbW + " " + h.toFixed(1));
      // The painted background is sized to the map's box; grow it too, or the
      // extra rows sit on whatever is behind the SVG.
      if (backdrop) backdrop.setAttribute("height", h.toFixed(1));
    }
    for (const t of tracks) t.el.setAttribute("d", trackD(t, m));

    for (const d of dots) {
      const lp = linXY(d.label, d.node);
      if (!lp) continue;
      d.el.setAttribute("cx", lerp(d.mapPt.x, lp.x, m).toFixed(1));
      d.el.setAttribute("cy", lerp(d.mapPt.y, lp.y, m).toFixed(1));
      // Interchanges are drawn larger on the map; on a row every stop is a stop.
      d.el.setAttribute("r", lerp(d.r, 3.6, m).toFixed(2));
    }

    for (const t of texts) {
      const lp = linXY(t.label, t.node);
      if (!lp) continue;
      const x = lerp(t.spec.x, lp.x + 7, m);
      const y = lerp(t.spec.y, lp.y - 9, m);
      const r0 = t.spec.rotate ? +t.spec.rotate[1] : 0;
      const rot = lerp(r0, -55, m);
      t.el.setAttribute("x", x.toFixed(1));
      t.el.setAttribute("y", y.toFixed(1));
      t.el.setAttribute("text-anchor", m > 0.5 ? "start" : t.spec.anchor);
      t.el.setAttribute("transform",
        "rotate(" + rot.toFixed(1) + " " + x.toFixed(1) + " " + y.toFixed(1) + ")");
      t.el.setAttribute("opacity", (t.primary ? 1 : m).toFixed(2));
    }

    sizeStage();
    for (const n of names) {
      const base = rowPos.get(n.label) || 0;
      n.el.setAttribute("x", (vbX + GUTTER - 14).toFixed(1));
      // In the chart a line occupies a whole band, so its name centres on it.
      const rowLine = vbY + LABEL_RISE + band * (base + 0.15);
      const rowBand = bandTop(n.label) + bandInner() / 2;
      n.el.setAttribute("y", lerp(rowLine, rowBand, sf).toFixed(1));
      n.el.setAttribute("opacity", Math.max(m, sf).toFixed(2));
    }
  }

  function draw() {
    const m = ease(view), sf = ease(str);
    if (dirty) { geometry(m, sf); dirty = false; }
    if (sf > 0) {
      const hx = timeX(now);
      playhead.setAttribute("x1", hx.toFixed(1));
      playhead.setAttribute("x2", hx.toFixed(1));
      playhead.setAttribute("y1", (vbY + LABEL_RISE - 24).toFixed(1));
      playhead.setAttribute("y2", (vbY + LABEL_RISE + band * nLines).toFixed(1));
    }

    const alive = new Set();
    let shown = 0;
    for (let i = lowerBound(now); i < trips.length; i++) {
      if (starts[i] > now) break;
      if (ends[i] < now) continue;
      const trip = trips[i];
      if (hidden.has(trip.r)) continue;
      const path = data.paths[trip.p];

      const f = stopAt(trip.k, now);
      const lo = Math.min(Math.floor(f), path.stops.length - 1);
      const hi = Math.min(lo + 1, path.stops.length - 1);
      const frac = f - lo;

      let x, y;
      if (m < 1) {
        const el = pathEls[trip.p];
        const len = lerp(path.stops[lo], path.stops[hi], frac);
        const pt = el.getPointAtLength(Math.min(len, el.getTotalLength()));
        x = pt.x; y = pt.y;
      }
      if (m > 0) {
        const a = linXY(trip.r, path.nodes[lo]), b = linXY(trip.r, path.nodes[hi]);
        if (a && b) {
          const lx = lerp(a.x, b.x, frac), ly = lerp(a.y, b.y, frac);
          x = m < 1 ? lerp(x, lx, m) : lx;
          y = m < 1 ? lerp(y, ly, m) : ly;
        } else if (m >= 1) {
          continue;   // no row for this stop; hide rather than draw a lie
        }
      }
      if (sf > 0) {
        // On the chart a train rides its own diagonal, at the playhead.
        const ya = stopY(trip.r, path.nodes[lo]), yb = stopY(trip.r, path.nodes[hi]);
        if (ya === null || yb === null) { if (sf >= 1) continue; }
        else {
          const sx = timeX(now);
          const sy = bandTop(trip.r) + lerp(ya, yb, frac);
          x = sf < 1 ? lerp(x, sx, sf) : sx;
          y = sf < 1 ? lerp(y, sy, sf) : sy;
        }
      }

      let dot = nodes.get(i);
      if (!dot) {
        dot = document.createElementNS(NS, "circle");
        dot.setAttribute("r", 5);
        dot.setAttribute("class", "train");
        dot.setAttribute("fill", data.lines[trip.r] || "#333");
        const title = document.createElementNS(NS, "title");
        title.textContent = trip.r + (trip.h ? " to " + trip.h : "");
        dot.appendChild(title);
        layer.appendChild(dot);
        nodes.set(i, dot);
      }
      dot.setAttribute("cx", x.toFixed(1));
      dot.setAttribute("cy", y.toFixed(1));
      alive.add(i);
      shown++;
    }
    for (const entry of nodes) {
      if (!alive.has(entry[0])) { entry[1].remove(); nodes.delete(entry[0]); }
    }
    clock.textContent = fmt(now);
    // The word is appended in CSS so a phone can drop it and keep the number.
    count.textContent = shown;
    count.setAttribute("aria-label", shown + (shown === 1 ? " train" : " trains") + " running");
  }

  function frame(ts) {
    const dt = Math.min((ts - last) / 1000, 0.25);
    last = ts;
    if (playing) {
      now += dt * speed;
      if (now > t1) now = t0;
      scrub.value = now;
    }
    // The view tween and the row tween both mark geometry dirty; once both have
    // settled the per-frame cost drops back to moving the trains.
    if (view !== viewTarget) {
      view += (viewTarget - view) * (reduced ? 1 : Math.min(dt * 3.2, 1));
      if (Math.abs(viewTarget - view) < 0.001) view = viewTarget;
      dirty = true;
    }
    if (str !== strTarget) {
      str += (strTarget - str) * (reduced ? 1 : Math.min(dt * 3.2, 1));
      if (Math.abs(strTarget - str) < 0.001) str = strTarget;
      dirty = true;
    }
    for (const entry of rowTarget) {
      const label = entry[0], target = entry[1];
      const cur = rowPos.get(label);
      if (cur !== target) {
        const next = reduced ? target : cur + (target - cur) * Math.min(dt * 6, 1);
        rowPos.set(label, Math.abs(target - next) < 0.002 ? target : next);
        dirty = true;
      }
    }
    draw();
    requestAnimationFrame(frame);
  }

  // ---------------------------------------------------------------- controls
  const playBtn = document.getElementById("play");
  playBtn.onclick = () => {
    playing = !playing;
    playBtn.textContent = playing ? "Pause" : "Play";
    playBtn.setAttribute("aria-pressed", String(playing));
  };
  scrub.oninput = () => { now = +scrub.value; draw(); };

  // One button showing the current rate, tapped to cycle. Four buttons for a
  // single value cost four times the width and said no more.
  const SPEEDS = [1, 60, 240, 900];
  let speedIdx = SPEEDS.indexOf(speed);
  if (speedIdx < 0) speedIdx = 1;
  const speedBtn = document.getElementById("speed");
  const showSpeed = () => {
    speed = SPEEDS[speedIdx];
    speedBtn.textContent = speed + "\u00d7";
    speedBtn.title = "Playback speed \u2014 tap to cycle";
  };
  speedBtn.onclick = () => { speedIdx = (speedIdx + 1) % SPEEDS.length; showSpeed(); };
  showSpeed();

  // The secondary controls, open where there is room and folded away on a
  // phone. Same markup either way.
  const moreBtn = document.getElementById("more");
  const morePanel = document.getElementById("more-panel");
  const setMore = open => {
    morePanel.hidden = !open;
    moreBtn.setAttribute("aria-expanded", String(open));
    moreBtn.innerHTML = open ? "Less &#9652;" : "More &#9662;";
    dirty = true;
  };
  moreBtn.onclick = () => setMore(morePanel.hidden);

  const labelsBtn = document.getElementById("labels-toggle");
  const fullBox = svg.getAttribute("viewBox");
  const tightBox = svg.getAttribute("data-viewbox-nolabels");
  let labelsOn = true;
  const syncBox = () => {
    // Tightening the box only makes sense for the map. Elsewhere the height is
    // owned by the morph, so leave it alone.
    if (mode !== "map") { dirty = true; return; }
    if (tightBox) svg.setAttribute("viewBox", labelsOn ? fullBox : tightBox);
  };
  labelsBtn.onclick = () => {
    labelsOn = !labelsOn;
    labelsBtn.setAttribute("aria-pressed", String(labelsOn));
    textLayer.style.display = labelsOn ? "" : "none";
    syncBox();
  };

  const sortGroup = document.getElementById("sort-group");
  const viewBtns = {
    map: document.getElementById("view-map"),
    linear: document.getElementById("view-linear"),
    string: document.getElementById("view-string"),
  };
  const setMode = next => {
    mode = next;
    viewTarget = next === "map" ? 0 : 1;
    strTarget = next === "string" ? 1 : 0;
    Object.keys(viewBtns).forEach(k =>
      viewBtns[k].setAttribute("aria-pressed", String(k === next)));
    // Station names belong to the network views; the chart has its own axis.
    sortGroup.hidden = next === "map";
    labelsBtn.hidden = next === "string";
    syncBox();
    dirty = true;
  };
  Object.keys(viewBtns).forEach(k => { viewBtns[k].onclick = () => setMode(k); });

  const sortBtns = {
    line: document.getElementById("sort-line"),
    size: document.getElementById("sort-size"),
  };
  const sized = new Map(layout.lines.map(l => [l.label, l.stations]));
  const applySort = how => {
    const sorted = order.slice().sort((a, b) =>
      how === "size" ? (sized.get(b) - sized.get(a)) || a.localeCompare(b)
                     : a.localeCompare(b, undefined, { numeric: true }));
    sorted.forEach((label, i) => rowTarget.set(label, i));
    Object.keys(sortBtns).forEach(k =>
      sortBtns[k].setAttribute("aria-pressed", String(k === how)));
    dirty = true;
  };
  sortBtns.line.onclick = () => applySort("line");
  sortBtns.size.onclick = () => applySort("size");
  applySort("line");
  for (const entry of rowTarget) rowPos.set(entry[0], entry[1]);

  const lines = document.getElementById("line-toggles");
  const lineCount = document.getElementById("line-count");
  const chips = new Map();

  // One place decides what a hidden line means, across all three views.
  const setRoute = (r, on) => {
    if (on) hidden.delete(r); else hidden.add(r);
    const off = on ? "" : "none";
    const chip = chips.get(r);
    if (chip) chip.setAttribute("aria-pressed", String(on));
    svg.querySelectorAll('#lines g.line[data-line="' + r + '"]')
       .forEach(g => g.style.display = off);
    for (const d of dots) if (d.label === r) d.el.style.display = off;
    for (const t of texts) if (t.label === r) t.el.style.display = off;
    for (const n of names) if (n.label === r) n.el.style.display = off;
    const sg = stringGroups.get(r);
    if (sg) sg.style.display = off;
    lineCount.textContent = (routes.length - hidden.size) + " of " + routes.length;
  };

  for (const r of routes) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.setAttribute("role", "button");
    chip.setAttribute("tabindex", "0");
    chip.setAttribute("aria-pressed", "true");
    chip.innerHTML = '<span class="dot" style="background:' +
      (data.lines[r] || "#333") + '"></span>' + r;
    const toggle = () => {
      setRoute(r, chip.getAttribute("aria-pressed") !== "true");
      draw();
    };
    chip.onclick = toggle;
    chip.onkeydown = e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    };
    chips.set(r, chip);
    lines.appendChild(chip);
  }
  document.getElementById("lines-all").onclick = () => {
    for (const r of routes) setRoute(r, true);
    draw();
  };
  document.getElementById("lines-none").onclick = () => {
    for (const r of routes) setRoute(r, false);
    draw();
  };
  lineCount.textContent = routes.length + " of " + routes.length;

  setMode("map");
  setMore(window.innerWidth > 900);
  addEventListener("resize", () => { dirty = true; });
  geometry(0, 0);
  requestAnimationFrame(frame);
})();
</script>
</html>
"""
