"""GTFS time and calendar handling."""

import datetime as dt

import pandas as pd
import pytest

from schematic.schedule import (Call, active_services, format_gtfs_time,
                                interpolate_calls, parse_gtfs_time)


def test_times_past_midnight_do_not_wrap():
    assert parse_gtfs_time("25:14:00") == 25 * 3600 + 14 * 60
    assert format_gtfs_time(90840) == "25:14:00"


def test_blank_times_are_none_not_zero():
    assert parse_gtfs_time("") is None
    assert parse_gtfs_time(None) is None
    assert parse_gtfs_time("garbage") is None
    assert parse_gtfs_time("00:00:00") == 0


def test_calendar_dates_exceptions_add_and_remove_service():
    tables = {
        "calendar": pd.DataFrame([{
            "service_id": "wk", "monday": "1", "tuesday": "1", "wednesday": "1",
            "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
            "start_date": "20260101", "end_date": "20261231"}]),
        "calendar_dates": pd.DataFrame([
            {"service_id": "wk", "date": "20260703", "exception_type": "2"},
            {"service_id": "hol", "date": "20260703", "exception_type": "1"},
        ]),
    }
    # An ordinary Friday.
    assert active_services(tables, dt.date(2026, 7, 10)) == {"wk"}
    # A holiday Friday: weekday service removed, holiday service added.
    assert active_services(tables, dt.date(2026, 7, 3)) == {"hol"}
    # A Saturday runs nothing here.
    assert active_services(tables, dt.date(2026, 7, 11)) == set()


def test_blank_intermediate_times_are_interpolated_evenly():
    calls = [Call("a", "n1", 0, 0), Call("b", "n2", None, None),
             Call("c", "n3", None, None), Call("d", "n4", 300, 300)]
    out = interpolate_calls(calls)
    assert [c.arrival for c in out] == [0, 100, 200, 300]


def test_service_window_from_calendar_dates_only():
    """calendar.txt is optional; some feeds are all exceptions."""
    from schematic.schedule import service_window
    tables = {
        "calendar_dates": pd.DataFrame([
            {"service_id": "a", "date": "20260302", "exception_type": "1"},
            {"service_id": "b", "date": "20260315", "exception_type": "1"},
            {"service_id": "c", "date": "20260401", "exception_type": "2"},
        ]),
    }
    assert service_window(tables) == (dt.date(2026, 3, 2), dt.date(2026, 3, 15))


def test_service_window_spans_both_tables():
    from schematic.schedule import service_window
    tables = {
        "calendar": pd.DataFrame([{
            "service_id": "wk", "monday": "1", "tuesday": "1", "wednesday": "1",
            "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
            "start_date": "20260401", "end_date": "20260630"}]),
        "calendar_dates": pd.DataFrame([
            {"service_id": "x", "date": "20260315", "exception_type": "1"}]),
    }
    assert service_window(tables) == (dt.date(2026, 3, 15), dt.date(2026, 6, 30))


def test_service_window_needs_at_least_one_table():
    from schematic.schedule import service_window
    with pytest.raises(ValueError, match="neither calendar"):
        service_window({})


def test_busiest_weekday_from_calendar_dates_only():
    from schematic.schedule import busiest_weekday
    tables = {
        "trips": pd.DataFrame([{"trip_id": f"t{i}", "route_id": "r", "service_id": "wk"}
                               for i in range(5)]
                              + [{"trip_id": "q", "route_id": "r", "service_id": "sat"}]),
        "calendar_dates": pd.DataFrame([
            # 2026-03-05 is a Thursday, 2026-03-07 a Saturday.
            {"service_id": "wk", "date": "20260305", "exception_type": "1"},
            {"service_id": "sat", "date": "20260307", "exception_type": "1"},
        ]),
    }
    assert busiest_weekday(tables) == dt.date(2026, 3, 5)
