"""What is in a feed, as data, before anything is laid out.

The exploration notebook (``01_fetch_and_explore``) reads a feed by hand:
which routes it has and how they are named, how many stops and of what
kind, when the calendar runs. The desktop app's Inspect screen needs the
same facts as data, plus the traps ``feeds`` and ``schedule`` already know
about real feeds, said as sentences a person can act on. ``inspect`` reads
the raw zip -- not the agency-filtered copy -- because its answer is what
the choice of mode and agency is made from.

It never reads ``stop_times``, which is most of a large feed: the trip
counts come from ``trips`` and the stop counts from ``stops``, so New York
inspects in a few seconds rather than a minute.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from . import feeds, schedule

# GTFS route_type, by number, and the LOOM mode name each maps to (what
# gtfs2graph's -m takes). The extended European codes (100-1700) are folded
# onto the basic set by their hundreds: 100 railway, 200 coach, 300 suburban
# rail, 400-600 urban rail, metro and underground, 700 bus, 800 trolleybus,
# 900 tram, 1000 water, 1200 ferry, 1300 aerial lift, 1400 funicular; air,
# taxi and the rest have no map and no mode.
ROUTE_TYPES: dict[int, tuple[str, str]] = {
    0: ("tram", "tram"), 1: ("subway", "subway"), 2: ("rail", "rail"),
    3: ("bus", "bus"), 4: ("ferry", "ferry"), 5: ("cable tram", "cablecar"),
    6: ("aerial lift", "gondola"), 7: ("funicular", "funicular"),
    11: ("trolleybus", "trolleybus"), 12: ("monorail", "monorail"),
}
EXTENDED: dict[int, int] = {1: 2, 2: 3, 3: 2, 4: 1, 5: 1, 6: 1, 7: 3, 8: 11, 9: 0,
                            10: 4, 12: 4, 13: 6, 14: 7}
# The other names gtfs2graph's -m takes for each basic type (feeds.MOTS is
# the whole list); a client that lets a person type a mode reads these to
# say what it keeps, rather than carrying the table itself.
ALIASES: dict[int, tuple[str, ...]] = {
    0: ("tram", "streetcar"), 1: ("subway", "metro"), 2: ("rail", "train"),
    3: ("bus", "coach"), 4: ("ferry", "boat", "ship"), 5: ("cablecar",),
    6: ("gondola",), 7: ("funicular",), 11: ("trolleybus", "trolley", "trolley-bus"),
    12: ("monorail", "mono-rail"),
}

# Rail-like types: a feed with nothing else is drawn whole ("all"), as the
# presets are, rather than filtered to its most common type.
RAILISH = frozenset({0, 1, 2, 5, 6, 7, 12})
# GTFS location_type, by number.
LOCATION_TYPES = {0: "stops", 1: "stations", 2: "entrances", 3: "generic_nodes",
                  4: "boarding_areas"}
# The tables the inspection reads: everything a map and a day need except
# stop_times, and the two that carry warnings.
TABLES = frozenset({"agency", "routes", "trips", "stops", "calendar", "calendar_dates",
                    "frequencies", "feed_info"})


def route_type_name(code: int) -> str:
    if code in ROUTE_TYPES:
        return ROUTE_TYPES[code][0]
    basic = EXTENDED.get(code // 100)
    if basic is not None:
        return f"{ROUTE_TYPES[basic][0]} (extended {code})"
    return f"type {code}"


def _basic(code: int) -> int | None:
    if code in ROUTE_TYPES:
        return code
    return EXTENDED.get(code // 100)


def mode_for(code: int) -> str | None:
    basic = _basic(code)
    return ROUTE_TYPES[basic][1] if basic is not None else None


def modes_for(code: int) -> list[str]:
    """Every -m name that keeps the type, the canonical one first."""
    basic = _basic(code)
    return list(ALIASES[basic]) if basic is not None else []


@dataclass
class Inspection:
    """A feed as the Inspect screen shows it. ``to_dict`` is the JSON shape."""

    key: str
    name: str
    tables: list[str]
    agencies: list[dict[str, str]]
    routes: list[dict[str, Any]]
    route_types: list[dict[str, Any]]
    stops: dict[str, int]
    trips: int
    frequency_trips: int
    service: dict[str, str] | None
    suggested_mode: str | None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _agencies(tables: dict[str, pd.DataFrame]) -> list[dict[str, str]]:
    df = tables.get("agency")
    if df is None or df.empty:
        return []
    out = []
    for _, row in df.iterrows():
        out.append({"agency_id": _text(row.get("agency_id")),
                    "agency_name": _text(row.get("agency_name"))})
    return out


def _routes(feed: feeds.Feed, tables: dict[str, pd.DataFrame]) -> list[dict[str, Any]]:
    routes = tables["routes"]
    trips = tables["trips"]
    per_route = trips.groupby("route_id").size() if len(trips) else pd.Series(dtype=int)
    labels = feeds.route_labels(feed, routes)
    out = []
    for i, (_, row) in enumerate(routes.iterrows()):
        route_id = _text(row.get("route_id"))
        out.append({
            "route_id": route_id,
            "agency_id": _text(row.get("agency_id")),
            "short_name": _text(row.get("route_short_name")),
            "long_name": _text(row.get("route_long_name")),
            "label": str(labels.iloc[i]),
            "route_type": _int(row.get("route_type"), -1),
            "color": _text(row.get("route_color")).upper() or None,
            "text_color": _text(row.get("route_text_color")).upper() or None,
            "trips": int(per_route.get(route_id, 0)),
        })
    return out


def _route_types(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_type: dict[int, dict[str, int]] = {}
    for route in routes:
        entry = by_type.setdefault(route["route_type"], {"routes": 0, "trips": 0})
        entry["routes"] += 1
        entry["trips"] += route["trips"]
    # The LOOM mode that keeps each type, so a client can show which of the
    # types a chosen mode draws without knowing the table itself.
    return [{"route_type": code, "name": route_type_name(code), "mode": mode_for(code),
             "modes": modes_for(code), **counts}
            for code, counts in sorted(by_type.items())]


def _stops(tables: dict[str, pd.DataFrame]) -> dict[str, int]:
    stops = tables["stops"]
    kinds = (stops["location_type"] if "location_type" in stops.columns
             else pd.Series([""] * len(stops), index=stops.index))
    codes = kinds.fillna("").map(lambda v: _int(v, 0) if _text(v) else 0)
    counts = codes.value_counts().to_dict()
    out = {name: int(counts.get(code, 0)) for code, name in LOCATION_TYPES.items()}
    out["total"] = len(stops)
    return out


def _suggest_mode(route_types: list[dict[str, Any]]) -> str | None:
    if not route_types:
        return None
    codes = {entry["route_type"] for entry in route_types}
    if codes <= RAILISH:
        return "all"
    railish = [entry for entry in route_types if entry["route_type"] in RAILISH]
    pool = railish or route_types
    best = max(pool, key=lambda entry: (entry["routes"], entry["trips"]))
    return mode_for(best["route_type"])


def inspect(key: str, *, anchor: dt.date | None = None) -> Inspection:
    """Read ``key``'s feed and say what is in it.

    ``anchor`` is the day the service choice scans from, as ``feeds.service``
    takes it; today when not given, and echoed in ``service``.
    """
    feed = feeds.get(key)
    anchor = anchor or dt.date.today()
    tables = feeds.tables(feed, normalized=False, only=TABLES)
    warnings: list[str] = []

    for stem in ("routes", "trips", "stops"):
        if stem not in tables:
            raise feeds.FeedError(f"{key!r} has no {stem}.txt, so there is nothing to inspect")

    agencies = _agencies(tables)
    routes = _routes(feed, tables)
    route_types = _route_types(routes)
    trips = tables["trips"]
    total_trips = len(trips)

    # Headway-based timetables: a trip in frequencies.txt is a template that
    # expand_trip turns into runs, so the count here understates the day.
    windows = schedule.frequency_windows(tables)
    frequency_trips = len(windows)
    if frequency_trips:
        warnings.append(
            f"The timetable is headway-based: {frequency_trips:,} of {total_trips:,} trips "
            f"are frequency templates that are expanded into runs, so a day has more "
            f"trains than the trip count suggests")

    unnamed = sum(1 for r in routes if not r["short_name"])
    if unnamed:
        warnings.append(
            f"{unnamed} of {len(routes)} routes have no route_short_name; their labels "
            f"come from route_long_name" + (
                " through the feed's label pattern" if feed.label_pattern else
                ", so set a label pattern if the long names are wordy"))

    if len(agencies) > 1 and not feed.agency:
        names = ", ".join(a["agency_name"] or a["agency_id"] for a in agencies)
        warnings.append(
            f"{len(agencies)} operators share this feed ({names}); set an agency to "
            f"keep one, or every operator's routes are drawn together")

    if "calendar" not in tables and "calendar_dates" in tables:
        warnings.append("The schedule is expressed as calendar_dates.txt exceptions only, "
                        "one row per service day; the window is the span of those days")

    service: dict[str, str] | None = None
    try:
        start, end = schedule.service_window(tables)
    except ValueError:
        warnings.append("The feed has neither calendar.txt nor calendar_dates.txt, so no "
                        "service day can be chosen and nothing can be animated")
    else:
        if end < anchor:
            warnings.append(
                f"The calendar ended on {end.isoformat()}, so no day after it has service; "
                f"the engine picks a day inside the window")
        elif start > anchor:
            warnings.append(
                f"The calendar starts on {start.isoformat()}; the engine picks a day inside "
                f"the window")
        try:
            day = schedule.busiest_weekday(tables, anchor=anchor)
        except ValueError as exc:
            warnings.append(f"No weekday in the window has service: {exc}")
        else:
            service = {"start": start.isoformat(), "end": end.isoformat(),
                       "busiest_weekday": day.isoformat(), "anchor": anchor.isoformat()}

    return Inspection(
        key=feed.key, name=feed.name, tables=sorted(tables),
        agencies=agencies, routes=routes, route_types=route_types,
        stops=_stops(tables), trips=total_trips, frequency_trips=frequency_trips,
        service=service, suggested_mode=_suggest_mode(route_types), warnings=warnings,
    )
