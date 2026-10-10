"""Where each line's track sits across an edge when lines differ in width
(issue 55, the note at the top of ``offsets.py``).

A line's slot is ``(w + 2c) * line_width``; line i sits at ``track_offset``
plus half the difference between the extra widths before and after it. The
two things that must hold are the criterion's numbers and that an edge whose
slots are all ``line_width`` is today's edge to the bit, which is what keeps
a map drawn without a width byte-identical.
"""

import random

import pytest

from schematic.offsets import slot_offsets, track_offset


def test_the_criterion_edge_places_its_lines_at_minus_9_1_and_plus_5_6():
    """At line_width 7 and line_gap 1.6 an edge of widths [1, 2] puts the
    narrow line at -9.1 and the wide one at +5.6: the wide one keeps its
    centre and the narrow one moves out by half its extra width."""
    line_width, gap = 7.0, 1.6
    spacing = line_width * gap
    assert slot_offsets([1 * line_width, 2 * line_width], line_width, spacing) == pytest.approx(
        [-9.1, 5.6], abs=1e-9)
    # The note's example: X beside Y(2) on one edge and beside Z(1) on the
    # next sits 3.5 apart at the node, a hop its round caps bridge.
    beside_z = slot_offsets([line_width, line_width], line_width, spacing)
    assert beside_z == pytest.approx([-5.6, 5.6], abs=1e-9)


def test_three_lines_sum_their_slots_and_the_gaps_between_them():
    """Slots of 7, 14 and 10.5 with gaps of 4.2 between them, centred: the
    bundle runs from -22.4 to 22.4 and each line sits in the middle of its own
    slot."""
    line_width, spacing = 7.0, 11.2
    offsets = slot_offsets([7.0, 14.0, 10.5], line_width, spacing)
    total = 7.0 + 14.0 + 10.5 + 2 * (spacing - line_width)
    edges = [-total / 2]
    for b in (7.0, 14.0, 10.5):
        edges.append(edges[-1] + b + (spacing - line_width))
    middles = [lo + b / 2 for lo, b in zip(edges, (7.0, 14.0, 10.5))]
    assert offsets == pytest.approx(middles, abs=1e-9)
    assert middles[-1] + 10.5 / 2 == pytest.approx(total / 2)


def test_an_edge_of_plain_slots_is_track_offset_bit_for_bit():
    """Over many random counts, widths and gaps, every slot line_width gives
    exactly what ``track_offset`` gives, compared as floats with ``==``: the
    correction is 0.0 and adding it changes no bit."""
    rng = random.Random(55)
    compared = 0
    for _ in range(20000):
        n = rng.randint(1, 12)
        line_width = rng.choice([7.0, 6.0, 4.0, 2.8, 1.0, 24.0, rng.uniform(1, 24)])
        spacing = line_width * rng.choice([1.6, 1.33, 2.0, 1.0, 3.0, rng.uniform(1, 3)])
        got = slot_offsets([line_width] * n, line_width, spacing)
        want = [track_offset(i, n, spacing) for i in range(n)]
        assert got == want, (n, line_width, spacing, got, want)
        assert [x.hex() for x in got] == [x.hex() for x in want]
        compared += n
    assert compared > 100000
