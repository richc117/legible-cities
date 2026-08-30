"""Offset and label-placement geometry -- the parts with real maths in them."""

import math

import pytest

from schematic.crs import to_mercator, to_wgs84
from schematic.labels import (Quad, Station, collide, label_quad, place,
                              polyline_quads)
from schematic.offsets import (cumulative_lengths, offset_polyline, point_at,
                               polyline_length, track_offset)


def test_offset_straight_line_is_exactly_parallel():
    assert offset_polyline([(0.0, 0.0), (10.0, 0.0)], 2.0) == [(0.0, 2.0), (10.0, 2.0)]
    assert offset_polyline([(0.0, 0.0), (10.0, 0.0)], -2.0) == [(0.0, -2.0), (10.0, -2.0)]


def test_offset_mitres_a_right_angle_onto_the_true_intersection():
    o = offset_polyline([(0.0, 0.0), (10.0, 0.0), (10.0, 10.0)], 2.0)
    assert o[1] == pytest.approx((8.0, 2.0))


def test_offset_mitres_a_45_degree_bend():
    # The octilinear case: the corner pulls back by distance * tan(22.5 deg).
    o = offset_polyline([(0.0, 0.0), (10.0, 0.0), (20.0, 10.0)], 2.0)
    assert o[1] == pytest.approx((10 - 2 * math.tan(math.radians(22.5)), 2.0))


def test_track_offsets_are_centred_on_the_edge():
    assert track_offset(0, 1, 8) == 0.0
    assert [track_offset(i, 3, 8) for i in range(3)] == [-8.0, 0.0, 8.0]
    assert [track_offset(i, 2, 8) for i in range(2)] == [-4.0, 4.0]


def test_arc_length_helpers():
    pl = [(0.0, 0.0), (3.0, 4.0), (3.0, 14.0)]
    assert polyline_length(pl) == pytest.approx(15.0)
    assert cumulative_lengths(pl) == pytest.approx([0.0, 5.0, 15.0])
    assert point_at(pl, 5.0) == pytest.approx((3.0, 4.0))
    assert point_at(pl, 10.0) == pytest.approx((3.0, 9.0))
    # Clamped rather than extrapolated at both ends.
    assert point_at(pl, -1.0) == pl[0]
    assert point_at(pl, 999.0) == pl[-1]


def test_mercator_round_trips():
    for c in [(-118.2437, 34.0522), (0.0, 0.0), (9.2157, 48.8030)]:
        assert to_wgs84(to_mercator(c)) == pytest.approx(c)


def test_rotated_labels_slide_past_each_other():
    """The reason placement uses oriented boxes rather than bounding boxes.

    Two long labels at 45 degrees, offset along x, are thin strips that do not
    overlap -- but their axis-aligned bounds do, heavily.
    """
    a = label_quad(0, 0, 90, 11, "start", -45)
    b = label_quad(40, 0, 90, 11, "start", -45)
    assert a.aabb[2] > b.aabb[0], "precondition: the bounding boxes should overlap"
    assert not collide(a, b)


def test_sat_basics():
    assert collide(Quad.rect(0, 0, 10, 10), Quad.rect(5, 5, 15, 15))
    assert not collide(Quad.rect(0, 0, 10, 10), Quad.rect(11, 0, 20, 10))
    assert collide(Quad.rect(0, 0, 10, 10), Quad.rect(11, 0, 20, 10), pad=2.0)


def test_dense_horizontal_run_places_every_label():
    """A long straight row of stations is the case that defeats naive placement."""
    stations = [Station(f"Station Number {i}", 100 + 40 * i, 200, on_horizontal_run=True)
                for i in range(20)]
    placed, dropped = place(stations, [], size=11, char_width=0.56, offset=9,
                            marker_radius=4.2)
    assert dropped == []
    assert len(placed) == 20
    for i, a in enumerate(placed):
        for b in placed[i + 1:]:
            assert not collide(a.quad, b.quad), f"{a.text} overlaps {b.text}"


def test_placer_prefers_clean_positions_before_crossing_a_line():
    """The over-the-line pass must not steal a slot a clean placement could use."""
    line = polyline_quads([(0.0, 300.0), (600.0, 300.0)], 8.0)
    stations = [Station("Alpha", 100, 100), Station("Beta", 300, 300)]
    placed, dropped = place(stations, line, size=11, char_width=0.56, offset=9,
                            marker_radius=4.2)
    assert dropped == []
    by_text = {p.text: p for p in placed}
    # Alpha is nowhere near the line and must be placed cleanly.
    assert not by_text["Alpha"].haloed
