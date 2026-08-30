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


# --------------------------------------------------------------------------
# Frequency-based feeds
# --------------------------------------------------------------------------

def _calls():
    from schematic.schedule import Call
    return [Call("a", "n1", 0, 0), Call("b", "n2", 120, 150), Call("c", "n3", 300, 300)]


def test_a_trip_with_no_window_is_left_alone():
    """Feeds mix timetabled and headway-based trips, so absolute times survive."""
    from schematic.schedule import expand_trip
    calls = _calls()
    out = expand_trip("T", calls, [])
    assert out == [("T", calls)]


def test_expansion_spaces_runs_by_the_headway():
    from schematic.schedule import Window, expand_trip
    runs = expand_trip("T", _calls(), [Window(3600, 3600 + 900, 300)])
    starts = [c[0].departure for _, c in runs]
    assert starts == [3600, 3900, 4200], starts
    # Nothing at or after end_time, which is how GTFS defines the last run.
    assert all(s < 3600 + 900 for s in starts)


def test_expansion_preserves_the_template_offsets():
    from schematic.schedule import Window, expand_trip
    template = _calls()
    _, calls = expand_trip("T", template, [Window(7200, 7500, 300)])[0]
    assert len(calls) == len(template)
    base = template[0].departure
    for a, b in zip(template, calls):
        assert b.arrival - 7200 == a.arrival - base
        assert b.departure - 7200 == a.departure - base
        assert b.stop_id == a.stop_id and b.node_id == a.node_id


def test_runs_get_unique_ids_across_windows():
    from schematic.schedule import Window, expand_trip
    runs = expand_trip("T", _calls(), [Window(0, 600, 300), Window(3600, 4200, 300)])
    ids = [tid for tid, _ in runs]
    assert len(ids) == len(set(ids)) == 4


def test_windows_are_expanded_in_time_order():
    from schematic.schedule import Window, expand_trip
    runs = expand_trip("T", _calls(), [Window(3600, 3900, 300), Window(0, 300, 300)])
    starts = [c[0].departure for _, c in runs]
    assert starts == sorted(starts)


def test_malformed_frequency_rows_are_ignored():
    from schematic.schedule import frequency_windows
    tables = {"frequencies": pd.DataFrame([
        {"trip_id": "ok", "start_time": "05:00:00", "end_time": "06:00:00", "headway_secs": "300"},
        {"trip_id": "zero", "start_time": "05:00:00", "end_time": "06:00:00", "headway_secs": "0"},
        {"trip_id": "backwards", "start_time": "06:00:00", "end_time": "05:00:00", "headway_secs": "300"},
        {"trip_id": "junk", "start_time": "05:00:00", "end_time": "06:00:00", "headway_secs": "x"},
    ])}
    w = frequency_windows(tables)
    assert set(w) == {"ok"}


def test_no_frequencies_table_is_fine():
    from schematic.schedule import frequency_windows
    assert frequency_windows({}) == {}
    assert frequency_windows({"frequencies": pd.DataFrame()}) == {}


def test_expired_feed_picks_a_date_inside_its_own_window():
    """Clamping to the nearest edge lands exactly on it -- New Year's Eve, for
    Mexico City. The middle of the window is an ordinary week by construction."""
    from schematic.schedule import busiest_weekday
    tables = {
        "trips": pd.DataFrame([{"trip_id": f"t{i}", "route_id": "r", "service_id": "wk"}
                               for i in range(3)]),
        "calendar": pd.DataFrame([{
            "service_id": "wk", "monday": "1", "tuesday": "1", "wednesday": "1",
            "thursday": "1", "friday": "1", "saturday": "0", "sunday": "0",
            "start_date": "20241201", "end_date": "20251231"}]),
    }
    d = busiest_weekday(tables)
    assert dt.date(2024, 12, 1) <= d <= dt.date(2025, 12, 31)
    assert d.weekday() < 5
    # Not pinned to either edge.
    assert d not in (dt.date(2024, 12, 1), dt.date(2025, 12, 31))
