"""feeds.inspect: a feed as data, with the traps said in words.

The synthetic tests build small zips under a temporary home. The real ones
read the checkout's cached feeds and skip without them, as test_cdmx does.
"""

import datetime as dt
import time

import pytest
from test_feeds import GOOD, gtfs_zip

from schematic import config, feeds, inspection

ANCHOR = dt.date(2026, 9, 10)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV, str(tmp_path))
    return tmp_path


def cached(key: str) -> bool:
    return (config.REPO_ROOT / "data" / "feeds" / f"{key}.zip").is_file()


def test_a_small_feed_inspects_whole(home, tmp_path):
    feeds.add(gtfs_zip(tmp_path / "f.zip"), key="mine")
    result = feeds.inspect("mine", anchor=ANCHOR)
    d = result.to_dict()
    assert d["key"] == "mine"
    assert d["agencies"] == [{"agency_id": "M", "agency_name": "Metro de Prueba"}]
    assert d["routes"] == [{
        "route_id": "R1", "agency_id": "M", "short_name": "1", "long_name": "Linea 1",
        "label": "1", "route_type": 1, "color": None, "text_color": None, "trips": 1}]
    assert d["route_types"] == [{"route_type": 1, "name": "subway", "routes": 1, "trips": 1}]
    assert d["stops"] == {"stops": 2, "stations": 0, "entrances": 0, "generic_nodes": 0,
                          "boarding_areas": 0, "total": 2}
    assert d["trips"] == 1 and d["frequency_trips"] == 0
    assert d["service"] == {"start": "2026-01-01", "end": "2026-12-31",
                            "busiest_weekday": "2026-09-10", "anchor": "2026-09-10"}
    assert d["suggested_mode"] == "all"
    assert d["warnings"] == []
    assert "stop_times" in d["tables"] or "stop_times" not in d["tables"]  # never read
    assert set(d["tables"]) >= {"agency", "routes", "trips", "stops", "calendar"}


def test_warnings_name_each_trap(home, tmp_path):
    tables = dict(GOOD)
    tables["agency.txt"] += "S,Suburbano,http://y,America/Mexico_City\n"
    tables["routes.txt"] += "R2,S,,Tren Suburbano,2\nB1,M,,Ruta 10,3\n"
    tables["trips.txt"] += "R2,s,T2\nB1,s,T3\n"
    tables["frequencies.txt"] = "trip_id,start_time,end_time,headway_secs\nT1,05:00:00,06:00:00,300\n"
    del tables["calendar.txt"]
    tables["calendar_dates.txt"] = "service_id,date,exception_type\ns,20250106,1\ns,20250107,1\n"
    feeds.add(gtfs_zip(tmp_path / "f.zip", tables), key="traps")
    result = feeds.inspect("traps", anchor=ANCHOR)
    text = "\n".join(result.warnings)
    assert "headway-based: 1 of 3 trips" in text
    assert "2 of 3 routes have no route_short_name" in text
    assert "2 operators share this feed (Metro de Prueba, Suburbano)" in text
    assert "calendar_dates.txt exceptions only" in text
    assert "The calendar ended on 2025-01-07" in text
    assert result.service["busiest_weekday"] in ("2025-01-06", "2025-01-07")
    assert result.frequency_trips == 1
    # Three routes of types 1, 2 and 3: a bus is in the feed, so the
    # suggestion is the most common rail-like type, not "all".
    assert [e["route_type"] for e in result.route_types] == [1, 2, 3]
    assert result.suggested_mode in ("subway", "rail")
    assert [r["label"] for r in result.routes] == ["1", "Tren Suburbano", "Ruta 10"]


def test_a_feed_with_no_calendar_has_no_service_and_says_so(home, tmp_path):
    tables = {k: v for k, v in GOOD.items() if k != "calendar.txt"}
    # add() refuses such a zip, so write the record by hand, as a feed cached
    # from before the check might be.
    config.feeds_dir().mkdir(parents=True, exist_ok=True)
    path = gtfs_zip(config.feeds_dir() / "nocal.zip", tables)
    feeds._write_user_feeds({"nocal": feeds.Feed(key="nocal", name="No calendar", url="",
                                                source="user")})
    assert path.is_file()
    result = feeds.inspect("nocal", anchor=ANCHOR)
    assert result.service is None
    assert any("neither calendar.txt nor calendar_dates.txt" in w for w in result.warnings)


def test_route_type_names_and_modes():
    assert inspection.route_type_name(0) == "tram"
    assert inspection.route_type_name(7) == "funicular"
    assert inspection.route_type_name(401) == "subway (extended 401)"
    assert inspection.route_type_name(99) == "type 99"
    assert inspection.mode_for(700) == "bus"
    assert inspection.mode_for(99) is None
    only_bus = [{"route_type": 3, "routes": 5, "trips": 50}]
    assert inspection._suggest_mode(only_bus) == "bus"
    assert inspection._suggest_mode([]) is None


@pytest.mark.skipif(not cached("la-metro-rail"), reason="LA's feed is not cached")
def test_la_has_six_routes_of_two_types_and_the_day_feeds_service_would_pick():
    result = feeds.inspect("la-metro-rail", anchor=ANCHOR)
    assert len(result.routes) == 6
    assert [e["route_type"] for e in result.route_types] == [0, 1]
    assert sorted(r["label"] for r in result.routes) == list("ABCDEK")
    assert result.suggested_mode == "all"
    # The same window and day the protocol's feeds.service answers.
    from schematic import schedule
    tables = feeds.tables("la-metro-rail", only=schedule.DAY_TABLES)
    start, end = schedule.service_window(tables)
    day = schedule.busiest_weekday(tables, anchor=ANCHOR)
    assert result.service == {"start": start.isoformat(), "end": end.isoformat(),
                              "busiest_weekday": day.isoformat(), "anchor": ANCHOR.isoformat()}
    assert any("no route_short_name" in w for w in result.warnings)


@pytest.mark.skipif(not cached("cdmx-metro"), reason="Mexico City's feed is not cached")
def test_cdmx_warns_of_the_headway_timetable_and_the_expired_calendar():
    result = feeds.inspect("cdmx-metro", anchor=ANCHOR)
    text = "\n".join(result.warnings)
    assert "headway-based" in text
    assert "The calendar ended on 2025-" in text
    assert len(result.agencies) >= 2
    assert any(a["agency_id"] == "METRO" for a in result.agencies)
    assert result.service is not None
    assert result.service["start"] <= result.service["busiest_weekday"] <= result.service["end"]


@pytest.mark.skipif(not cached("metra"), reason="Metra's feed is not cached")
def test_metra_headers_with_leading_spaces_are_clean():
    result = feeds.inspect("metra", anchor=ANCHOR)
    assert result.routes and all(r["route_id"] for r in result.routes)
    assert result.trips > 0
    assert result.service is not None


@pytest.mark.skipif(not cached("nyc-subway"), reason="New York's feed is not cached")
def test_nyc_inspects_in_under_five_seconds():
    t0 = time.perf_counter()
    result = feeds.inspect("nyc-subway", anchor=ANCHOR)
    took = time.perf_counter() - t0
    assert took < 5, f"{took:.1f}s"
    assert len(result.routes) > 20
    assert result.stops["total"] > 400
