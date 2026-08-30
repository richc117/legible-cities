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
from dataclasses import dataclass, field
from pathlib import Path

from .linegraph import LineGraph
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
    # Trips that lost at least one call because its stop matched no node.
    trips_with_skipped_calls: int = 0
    # Trips routed over another line's track for at least one hop.
    trips_with_borrowed_track: int = 0

    def to_json(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "lines": self.lines,
            "paths": self.paths,
            "trips": self.trips,
        }


def build(render: RenderResult, graph: LineGraph, trips: list[Trip],
          date: dt.date) -> Animation:
    """Route every trip and collect the deduplicated paths."""
    net = RouteNetwork.build(render)

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
                          "borrowed": tp.borrowed_hops})
        if paths[idx]["borrowed"]:
            borrowed += 1
        stops = paths[idx]["stops"]
        if len(stops) != len(calls):
            unrouted.append(trip.trip_id)
            continue

        # Keyframes: arrive at a stop, then hold there until departure. The dwell
        # pair is what stops trains gliding through stations without pausing.
        keys: list[list[float]] = []
        for call, length in zip(calls, stops):
            keys.append([call.arrival, length])
            if call.departure > call.arrival:
                keys.append([call.departure, length])
        out_trips.append({"p": idx, "r": trip.route_label, "h": trip.headsign, "k": keys})

    out_trips.sort(key=lambda t: t["k"][0][0])
    return Animation(date=date, paths=paths, trips=out_trips, lines=colors,
                     unrouted=unrouted, trips_with_skipped_calls=skipped_calls,
                     trips_with_borrowed_track=borrowed)


def write(animation: Animation, svg: str, out_dir: Path, *,
          title: str = "Transit animation") -> tuple[Path, Path]:
    """Write positions.json and a self-contained animation.html."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data = animation.to_json()

    json_path = out_dir / "positions.json"
    json_path.write_text(json.dumps(data, separators=(",", ":")))

    html_path = out_dir / "animation.html"
    html_path.write_text(_HTML.replace("__TITLE__", title)
                              .replace("__SVG__", svg)
                              .replace("__DATA__", json.dumps(data, separators=(",", ":"))))
    return json_path, html_path


_HTML = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg: #f6f6f4; --panel: #ffffff; --ink: #16181d; --muted: #6b7280;
    --border: #e2e2df; --accent: #16181d;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #14161a; --panel: #1c1f25; --ink: #f2f3f5; --muted: #9aa1ac;
            --border: #2c313a; --accent: #f2f3f5; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
  header { display: flex; flex-wrap: wrap; gap: 14px 20px; align-items: center;
           padding: 12px 18px; background: var(--panel);
           border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 5; }
  h1 { font-size: 15px; font-weight: 600; margin: 0 12px 0 0; letter-spacing: -0.01em; }
  .clock { font-variant-numeric: tabular-nums; font-size: 22px; font-weight: 600;
           min-width: 5.5ch; letter-spacing: -0.02em; }
  .sub { color: var(--muted); font-size: 12px; }
  .group { display: flex; align-items: center; gap: 8px; }
  button { font: inherit; color: var(--ink); background: var(--panel);
           border: 1px solid var(--border); border-radius: 7px; padding: 5px 11px;
           cursor: pointer; }
  button:hover { border-color: var(--muted); }
  button[aria-pressed="true"] { background: var(--accent); color: var(--panel);
                                border-color: var(--accent); }
  input[type=range] { width: min(420px, 42vw); accent-color: var(--accent); }
  .lines { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
          border: 1px solid var(--border); border-radius: 999px; cursor: pointer;
          background: var(--panel); user-select: none; }
  .chip .dot { width: 10px; height: 10px; border-radius: 50%; }
  .chip[aria-pressed="false"] { opacity: 0.38; }
  #stage { padding: 16px; }
  svg { width: 100%; height: auto; display: block;
        background: var(--panel); border: 1px solid var(--border); border-radius: 10px; }
  .train { paint-order: stroke; stroke: #fff; stroke-width: 1.6; }
  @media (prefers-color-scheme: dark) {
    svg { filter: invert(1) hue-rotate(180deg); }
    .train { stroke: #000; }
  }
</style>
<header>
  <h1>__TITLE__</h1>
  <div class="group">
    <button id="play" aria-pressed="true">Pause</button>
    <div class="clock" id="clock">--:--</div>
  </div>
  <div class="group">
    <input type="range" id="scrub" min="0" max="1" step="1" value="0">
    <span class="sub" id="count">0 trains</span>
  </div>
  <div class="group" id="speeds"></div>
  <div class="lines" id="line-toggles"></div>
</header>
<main id="stage">__SVG__</main>
<script id="data" type="application/json">__DATA__</script>
<script>
(() => {
  const data = JSON.parse(document.getElementById("data").textContent);
  const svg = document.querySelector("#stage svg");
  const NS = "http://www.w3.org/2000/svg";

  // Hidden reference geometry: getPointAtLength() needs real path elements, but
  // these are the routing paths, not the drawn map, so they must never paint.
  const defs = document.createElementNS(NS, "defs");
  const pathEls = data.paths.map(p => {
    const el = document.createElementNS(NS, "path");
    el.setAttribute("d", p.d);
    defs.appendChild(el);
    return el;
  });
  svg.appendChild(defs);

  const layer = svg.querySelector("#trains") || svg.appendChild(document.createElementNS(NS, "g"));

  const t0 = Math.min(...data.trips.map(t => t.k[0][0]));
  const t1 = Math.max(...data.trips.map(t => t.k[t.k.length - 1][0]));

  // Trips sorted by start time; a moving window keeps the per-frame scan to the
  // few dozen trains actually running instead of the whole service day.
  const trips = data.trips;
  const starts = trips.map(t => t.k[0][0]);
  const ends = trips.map(t => t.k[t.k.length - 1][0]);
  const maxSpan = Math.max(...trips.map((t, i) => ends[i] - starts[i]));

  const routes = [...new Set(trips.map(t => t.r))].sort();
  const hidden = new Set();
  const nodes = new Map();   // trip index -> <circle>

  let now = 7 * 3600, speed = 60, playing = true, last = performance.now();

  const clock = document.getElementById("clock");
  const count = document.getElementById("count");
  const scrub = document.getElementById("scrub");
  scrub.min = t0; scrub.max = t1; scrub.value = now;

  const fmt = s => {
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return String(h % 24).padStart(2, "0") + ":" + String(m).padStart(2, "0")
         + (h >= 24 ? " +1d" : "");
  };

  // Distance along the path at time t: find the bracketing keyframes and lerp.
  const lengthAt = (keys, t) => {
    let lo = 0, hi = keys.length - 1;
    if (t <= keys[0][0]) return keys[0][1];
    if (t >= keys[hi][0]) return keys[hi][1];
    while (hi - lo > 1) {
      const mid = (lo + hi) >> 1;
      if (keys[mid][0] <= t) lo = mid; else hi = mid;
    }
    const [ta, la] = keys[lo], [tb, lb] = keys[hi];
    return tb === ta ? lb : la + (lb - la) * (t - ta) / (tb - ta);
  };

  // First trip that could still be running at time t.
  const lowerBound = t => {
    let lo = 0, hi = starts.length;
    const cutoff = t - maxSpan;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (starts[mid] < cutoff) lo = mid + 1; else hi = mid; }
    return lo;
  };

  function draw() {
    const alive = new Set();
    let shown = 0;
    for (let i = lowerBound(now); i < trips.length; i++) {
      if (starts[i] > now) break;
      if (ends[i] < now) continue;
      const trip = trips[i];
      if (hidden.has(trip.r)) continue;
      const el = pathEls[trip.p];
      const total = el.getTotalLength();
      const len = Math.min(lengthAt(trip.k, now), total);
      const pt = el.getPointAtLength(len);

      let dot = nodes.get(i);
      if (!dot) {
        dot = document.createElementNS(NS, "circle");
        dot.setAttribute("r", 5);
        dot.setAttribute("class", "train");
        dot.setAttribute("fill", data.lines[trip.r] || "#333");
        const t = document.createElementNS(NS, "title");
        t.textContent = trip.r + (trip.h ? " to " + trip.h : "");
        dot.appendChild(t);
        layer.appendChild(dot);
        nodes.set(i, dot);
      }
      dot.setAttribute("cx", pt.x.toFixed(1));
      dot.setAttribute("cy", pt.y.toFixed(1));
      alive.add(i);
      shown++;
    }
    for (const [i, el] of nodes) if (!alive.has(i)) { el.remove(); nodes.delete(i); }
    clock.textContent = fmt(now);
    count.textContent = shown + (shown === 1 ? " train" : " trains");
  }

  function frame(ts) {
    const dt = Math.min((ts - last) / 1000, 0.25);
    last = ts;
    if (playing) {
      now += dt * speed;
      if (now > t1) now = t0;
      scrub.value = now;
    }
    draw();
    requestAnimationFrame(frame);
  }

  // -- controls --
  const playBtn = document.getElementById("play");
  playBtn.onclick = () => {
    playing = !playing;
    playBtn.textContent = playing ? "Pause" : "Play";
    playBtn.setAttribute("aria-pressed", String(playing));
  };
  scrub.oninput = () => { now = +scrub.value; draw(); };

  const speeds = document.getElementById("speeds");
  for (const [label, mult] of [["1x", 1], ["60x", 60], ["240x", 240], ["900x", 900]]) {
    const b = document.createElement("button");
    b.textContent = label;
    b.setAttribute("aria-pressed", String(mult === speed));
    b.onclick = () => {
      speed = mult;
      [...speeds.children].forEach(c => c.setAttribute("aria-pressed", String(c === b)));
    };
    speeds.appendChild(b);
  }

  // Named line-toggles, not lines: the inlined SVG already owns that id for
  // its drawn line group, and duplicate ids in one document are invalid.
  const lines = document.getElementById("line-toggles");
  for (const r of routes) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.setAttribute("aria-pressed", "true");
    chip.innerHTML = `<span class="dot" style="background:${data.lines[r] || "#333"}"></span>${r}`;
    chip.onclick = () => {
      const on = chip.getAttribute("aria-pressed") === "true";
      chip.setAttribute("aria-pressed", String(!on));
      if (on) hidden.add(r); else hidden.delete(r);
      svg.querySelectorAll(`#lines g.line[data-line="${r}"]`)
         .forEach(g => g.style.display = on ? "none" : "");
      draw();
    };
    lines.appendChild(chip);
  }

  requestAnimationFrame(frame);
})();
</script>
</html>
"""
