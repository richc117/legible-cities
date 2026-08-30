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
