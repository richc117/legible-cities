"""Turn a GTFS schedule into timed trips on the schematic map.

The chain is: pick a service date -> find the trips running that day -> read each
trip's ordered stops and times -> map those stops onto schematic nodes -> walk
the schematic graph between consecutive stops to get a drawn path.

GTFS quirks handled here:

* Times run past midnight (``25:14:00`` is 1:14am the next day) and are stored as
  seconds after that service day's midnight, never wrapped.
* Trips call at platform stops (``location_type=0``); the map draws stations.
  Platforms are folded into their ``parent_station`` before matching.
* ``calendar`` plus ``calendar_dates`` exceptions decide what actually runs.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass

import pandas as pd

from .linegraph import LineGraph
from .names import normalize_name

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_gtfs_time(value: str) -> int | None:
    """``'25:14:00'`` -> seconds after service-day midnight. None if blank."""
    if not isinstance(value, str) or not value.strip():
        return None
    parts = value.strip().split(":")
    if len(parts) != 3:
        return None
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def format_gtfs_time(seconds: int) -> str:
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def active_services(tables: dict[str, pd.DataFrame], date: dt.date) -> set[str]:
    """service_ids running on ``date``, applying calendar_dates exceptions."""
    stamp = date.strftime("%Y%m%d")
    active: set[str] = set()

    cal = tables.get("calendar")
    if cal is not None and len(cal):
        day = _WEEKDAYS[date.weekday()]
        in_range = (cal["start_date"] <= stamp) & (cal["end_date"] >= stamp)
        active |= set(cal.loc[in_range & (cal[day] == "1"), "service_id"])

    exc = tables.get("calendar_dates")
    if exc is not None and len(exc):
        today = exc[exc["date"] == stamp]
        active |= set(today.loc[today["exception_type"] == "1", "service_id"])
        active -= set(today.loc[today["exception_type"] == "2", "service_id"])
    return active


def busiest_weekday(tables: dict[str, pd.DataFrame],
                    lines: set[str] | None = None) -> dt.date:
    """A representative service date: the weekday in the feed window with the most trips."""
    trips = tables["trips"]
    if lines is not None:
        trips = trips[trips["route_id"].isin(routes_matching(tables, lines))]
    per_service = trips.groupby("service_id").size()
    cal = tables.get("calendar")
    if cal is None or not len(cal):
        raise ValueError("feed has no calendar.txt; pass an explicit date")
    start = dt.datetime.strptime(cal["start_date"].min(), "%Y%m%d").date()
    end = dt.datetime.strptime(cal["end_date"].max(), "%Y%m%d").date()
    # Scan at most a month; feed windows are usually a season and the pattern repeats.
    best, best_n = None, -1
    for i in range((min(end, start + dt.timedelta(days=30)) - start).days + 1):
        day = start + dt.timedelta(days=i)
        if day.weekday() > 4:
            continue
        n = int(per_service.reindex(list(active_services(tables, day))).fillna(0).sum())
        if n > best_n:
            best, best_n = day, n
    if best is None:
        raise ValueError("no weekday with service found in the feed window")
    return best


# --------------------------------------------------------------------------
# Matching GTFS stops to schematic nodes
# --------------------------------------------------------------------------

@dataclass
class StopMatch:
    """Resolution of GTFS stop_ids onto schematic node ids."""

    stop_to_node: dict[str, str]
    unmatched: list[str]
    by_id: int = 0
    by_parent: int = 0
    by_name: int = 0

    @property
    def coverage(self) -> float:
        total = len(self.stop_to_node) + len(self.unmatched)
        return len(self.stop_to_node) / total if total else 0.0

    def report(self) -> str:
        return (f"matched {len(self.stop_to_node)}/{len(self.stop_to_node) + len(self.unmatched)} "
                f"stops ({self.coverage:.0%}) "
                f"[station_id={self.by_id}, parent_station={self.by_parent}, name={self.by_name}]"
                + (f"; unmatched: {self.unmatched[:8]}" if self.unmatched else ""))


def stop_line_labels(tables: dict[str, pd.DataFrame], labels: set[str]) -> dict[str, set[str]]:
    """stop_id -> the set of in-scope line labels calling there."""
    names = route_labels(tables)
    trips = tables["trips"]
    trips = trips[trips["route_id"].isin(routes_matching(tables, labels))]
    trip_line = dict(zip(trips["trip_id"], trips["route_id"].map(names)))
    st = tables["stop_times"]
    st = st[st["trip_id"].isin(trip_line)]
    out: dict[str, set[str]] = {}
    for stop_id, trip_id in zip(st["stop_id"], st["trip_id"]):
        out.setdefault(stop_id, set()).add(trip_line[trip_id])
    return out


def match_stops(graph: LineGraph, tables: dict[str, pd.DataFrame]) -> StopMatch:
    """Map every GTFS stop_id used by a trip to a schematic node id.

    Tried in order of trustworthiness: the node's ``station_id`` verbatim, the
    stop's ``parent_station``, then a normalised name match. Anything left over
    is reported rather than silently dropped -- an unmatched stop means a train
    with a hole in its path.

    Parent and name lookups can be ambiguous, and picking arbitrarily is not
    harmless: BART has two separate Coliseum stations, the mainline one and the
    Oakland Airport Connector platform, and sending mainline trains to the
    airport platform strands every one of them in a two-node island. So
    ambiguity is broken by which lines each candidate actually carries.
    """
    stops = tables["stops"].set_index("stop_id")
    labels = set(graph.labels)
    used = sorted(served_stop_ids(tables, labels))
    parent_of = stops["parent_station"] if "parent_station" in stops.columns else None
    stop_lines = stop_line_labels(tables, labels)

    node_lines: dict[str, set[str]] = {}
    degree: dict[str, int] = {}
    for e in graph.edges:
        for end in (e.src, e.dst):
            node_lines.setdefault(end, set()).update(ln.label for ln in e.lines)
            degree[end] = degree.get(end, 0) + 1

    by_station_id: dict[str, str] = {}
    # topo merges the two platform stops at an interchange into one node and
    # keeps only one of their stop_ids, so the other platform matches nothing by
    # id. Index the graph by each node's parent station too -- that is what the
    # orphaned platform shares with the one that survived, and it is a firmer
    # link than comparing names.
    by_parent: dict[str, list[str]] = {}
    by_norm_name: dict[str, list[str]] = {}
    for node in graph.stations:
        if node.station_id:
            sid = str(node.station_id)
            by_station_id.setdefault(sid, node.id)
            parent = parent_of.get(sid) if parent_of is not None else None
            if isinstance(parent, str) and parent:
                by_parent.setdefault(parent, []).append(node.id)
        if node.station_label:
            by_norm_name.setdefault(normalize_name(node.station_label), []).append(node.id)

    def best(candidates: list[str], stop_id: str) -> str | None:
        """Pick the candidate whose lines best explain the stop's own services."""
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        wanted = stop_lines.get(stop_id, set())
        return max(candidates,
                   key=lambda n: (len(wanted & node_lines.get(n, set())), degree.get(n, 0)))

    m = StopMatch(stop_to_node={}, unmatched=[])
    for sid in used:
        if sid in by_station_id:
            m.stop_to_node[sid] = by_station_id[sid]
            m.by_id += 1
            continue
        row = stops.loc[sid] if sid in stops.index else None
        parent = row.get("parent_station") if row is not None else None
        node_id = None
        if isinstance(parent, str):
            pool = ([by_station_id[parent]] if parent in by_station_id else []) \
                + by_parent.get(parent, [])
            node_id = best(pool, sid)
        if node_id:
            m.stop_to_node[sid] = node_id
            m.by_parent += 1
            continue
        name = row.get("stop_name") if row is not None else None
        node_id = best(by_norm_name.get(normalize_name(name), []), sid) \
            if isinstance(name, str) else None
        if node_id:
            m.stop_to_node[sid] = node_id
            m.by_name += 1
            continue
        m.unmatched.append(sid)
    return m


# --------------------------------------------------------------------------
# Trips
# --------------------------------------------------------------------------

@dataclass
class Call:
    """One scheduled stop on a trip."""

    stop_id: str
    node_id: str | None
    arrival: int
    departure: int


@dataclass
class Trip:
    trip_id: str
    route_label: str
    headsign: str
    calls: list[Call]

    @property
    def start(self) -> int:
        return self.calls[0].departure

    @property
    def end(self) -> int:
        return self.calls[-1].arrival


def routes_matching(tables: dict[str, pd.DataFrame], labels: set[str]) -> set[str]:
    """route_ids whose label appears in ``labels``.

    The line graph is built with a mode filter (``gtfs2graph -m tram``), so a
    combined feed's bus routes never reach the map. The schedule has to be
    filtered the same way or it drags in tens of thousands of trips that can
    never be routed, and drowns the stop matcher in stops the map has no node
    for. The graph's own line labels are the authority on what is in scope.
    """
    return {rid for rid, label in route_labels(tables).items() if label in labels}


def served_stop_ids(tables: dict[str, pd.DataFrame], labels: set[str]) -> set[str]:
    """Stops called at by the routes in ``labels``."""
    route_ids = routes_matching(tables, labels)
    trips = tables["trips"]
    trip_ids = set(trips.loc[trips["route_id"].isin(route_ids), "trip_id"])
    st = tables["stop_times"]
    return set(st.loc[st["trip_id"].isin(trip_ids), "stop_id"])


def route_labels(tables: dict[str, pd.DataFrame]) -> dict[str, str]:
    """route_id -> the label LOOM will have used (short name, else long name)."""
    r = tables["routes"]
    out: dict[str, str] = {}
    for _, row in r.iterrows():
        short = row.get("route_short_name")
        long = row.get("route_long_name")
        label = short if isinstance(short, str) and short.strip() else long
        out[row["route_id"]] = str(label).strip() if isinstance(label, str) else str(row["route_id"])
    return out


def interpolate_calls(calls: list[Call]) -> list[Call]:
    """Fill blank intermediate times by spreading them evenly between known ones."""
    known = [i for i, c in enumerate(calls) if c.arrival is not None]
    if len(known) < 2:
        return calls
    for a, b in zip(known, known[1:]):
        gap = b - a
        if gap < 2:
            continue
        t0, t1 = calls[a].departure, calls[b].arrival
        for k in range(1, gap):
            t = t0 + (t1 - t0) * k / gap
            calls[a + k].arrival = calls[a + k].departure = int(round(t))
    return calls


def trips_on(tables: dict[str, pd.DataFrame], date: dt.date, match: StopMatch,
             lines: set[str] | None = None) -> list[Trip]:
    """Every trip running on ``date``, with times resolved and stops mapped.

    ``lines`` restricts the result to routes carrying those labels -- pass the
    line graph's labels so a combined bus-and-rail feed yields only the trips
    the map can actually show.
    """
    services = active_services(tables, date)
    trips_df = tables["trips"]
    trips_df = trips_df[trips_df["service_id"].isin(services)]
    if lines is not None:
        trips_df = trips_df[trips_df["route_id"].isin(routes_matching(tables, lines))]
    labels = route_labels(tables)

    st = tables["stop_times"]
    st = st[st["trip_id"].isin(set(trips_df["trip_id"]))].copy()
    st["seq"] = st["stop_sequence"].astype(int)
    st = st.sort_values(["trip_id", "seq"])

    arr = st["arrival_time"].map(parse_gtfs_time)
    dep = st["departure_time"].map(parse_gtfs_time)
    st["arr"] = arr.fillna(dep)
    st["dep"] = dep.fillna(arr)

    meta = trips_df.set_index("trip_id")
    grouped: dict[str, list[Call]] = defaultdict(list)
    for trip_id, stop_id, a, d in zip(st["trip_id"], st["stop_id"], st["arr"], st["dep"]):
        grouped[trip_id].append(Call(
            stop_id=stop_id,
            node_id=match.stop_to_node.get(stop_id),
            arrival=None if pd.isna(a) else int(a),
            departure=None if pd.isna(d) else int(d),
        ))

    out: list[Trip] = []
    for trip_id, calls in grouped.items():
        calls = interpolate_calls(calls)
        calls = [c for c in calls if c.arrival is not None and c.departure is not None]
        if len(calls) < 2:
            continue
        row = meta.loc[trip_id]
        headsign = row.get("trip_headsign")
        out.append(Trip(
            trip_id=trip_id,
            route_label=labels.get(row["route_id"], str(row["route_id"])),
            headsign=headsign if isinstance(headsign, str) else "",
            calls=calls,
        ))
    out.sort(key=lambda t: t.start)
    return out


def concurrent_trips(trips: list[Trip], at_second: int) -> list[Trip]:
    """Trips in motion at a given time -- used to sanity-check the animation."""
    return [t for t in trips if t.start <= at_second <= t.end]
