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
          stem: str = "animation", title: str = "Transit animation",
          name: str | None = None, subtitle: str = "",
          back: str = "index.html") -> tuple[Path, Path]:
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
        _HTML.replace("__TITLE__", html_escape(title))
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
    display: flex; flex-wrap: wrap; gap: 0.7rem 1.4rem; align-items: baseline;
    padding: 0.9rem 1.4rem; background: var(--bg);
    border-bottom: 1px solid var(--border); position: sticky; top: 0; z-index: 5;
  }
  h1 { font-size: 1.05rem; font-weight: 700; margin: 0; }
  .back { font-size: 0.86rem; color: var(--muted); text-decoration: none;
          white-space: nowrap; }
  .back:hover { color: var(--text); }
  .clock { font-variant-numeric: tabular-nums; font-size: 1.35rem; min-width: 5.5ch; }
  .sub { color: var(--muted); font-size: 0.86rem; }
  .group { display: flex; align-items: center; gap: 0.6rem; }
  button {
    font: inherit; font-size: 0.86rem; color: var(--muted); background: none;
    border: 0; border-bottom: 1px solid transparent; padding: 0.1rem 0;
    cursor: pointer;
  }
  button:hover { color: var(--text); }
  button[aria-pressed="true"] { color: var(--text); border-bottom-color: var(--text); }
  input[type=range] { width: min(380px, 40vw); accent-color: var(--text); }
  .lines { display: flex; gap: 0.7rem; flex-wrap: wrap; }
  .chip {
    display: inline-flex; align-items: center; gap: 0.35rem; cursor: pointer;
    user-select: none; font-size: 0.86rem; color: var(--muted);
  }
  .chip:hover { color: var(--text); }
  .chip .dot { width: 9px; height: 9px; border-radius: 50%; }
  .chip[aria-pressed="true"] { color: var(--text); }
  .chip[aria-pressed="false"] .dot { opacity: 0.3; }
  #stage { padding: 1.4rem; }
  svg { width: 100%; height: auto; display: block; }
  @media (max-width: 900px) {
    /* A wide network squeezed into a phone is unreadable. Let the map keep a
       usable size and pan inside its own container instead -- the page itself
       must never scroll sideways. */
    #stage { overflow-x: auto; -webkit-overflow-scrolling: touch; }
    #stage svg { width: auto; height: 62vh; min-width: 100%; }
  }
  /* Painted behind the glyph so a train reads against its own line colour. */
  .train { paint-order: stroke; stroke: var(--train-halo); stroke-width: 1.6; }
  @media (max-width: 640px) {
    html, body { font-size: 17px; }
    header { padding: 0.8rem 1rem; gap: 0.5rem 1rem; }
    h1 { font-size: 1rem; }
    .clock { font-size: 1.15rem; }
    #stage { padding: 0.9rem 0; }
  }
</style>
<header>
  <div class="group">
    <a class="back" href="__BACK__">&larr; Atlas</a>
    <h1>__NAME__</h1>
    <span class="sub">__SUBTITLE__</span>
  </div>
  <div class="group">
    <button id="play" aria-pressed="true">Pause</button>
    <div class="clock" id="clock">--:--</div>
  </div>
  <div class="group">
    <input type="range" id="scrub" min="0" max="1" step="1" value="0">
    <span class="sub" id="count">0 trains</span>
  </div>
  <div class="group" id="speeds"></div>
  <div class="group">
    <button id="labels-toggle" aria-pressed="true">Labels</button>
  </div>
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

  // Station names crowd a dense network, and following a moving train is much
  // easier without them. The group is only hidden, never removed, so the labels
  // the placer solved for come straight back untouched.
  //
  // The canvas is sized to fit the labels, which reach outside the track
  // geometry, so hiding them also tightens the viewBox onto the network alone
  // -- the renderer supplies both boxes. Worth up to a fifth of the canvas on
  // networks with long station names.
  const labelsBtn = document.getElementById("labels-toggle");
  const labelGroup = svg.querySelector("#labels");
  const fullBox = svg.getAttribute("viewBox");
  const tightBox = svg.getAttribute("data-viewbox-nolabels");
  if (!labelGroup) {
    labelsBtn.remove();          // map was rendered with labels=False
  } else {
    labelsBtn.onclick = () => {
      const on = labelsBtn.getAttribute("aria-pressed") !== "true";
      labelsBtn.setAttribute("aria-pressed", String(on));
      labelGroup.style.display = on ? "" : "none";
      if (tightBox) svg.setAttribute("viewBox", on ? fullBox : tightBox);
    };
  }

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
