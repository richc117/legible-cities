"""How a train is drawn: the dot's radius and the trail behind it (issue 75).

``dot_radius`` and ``trail`` are the animation's, not the map's, so they ride
in the page's data and never in the SVG. Each is written only when it is not
the default, the way ``names`` is, so a map built without them carries the
data it always did, byte for byte.
"""

import datetime as dt
import json
import re

import pytest
from test_colors import _graph

from schematic import animate
from schematic.render import render

DAY = dt.date(2026, 9, 10)


def _built(**kwargs) -> animate.Animation:
    graph = _graph([[("A", "0072bc")]])
    return animate.build(render(graph, labels=False), graph, [], DAY, **kwargs)


def test_a_page_built_without_them_carries_neither_key():
    data = _built().to_json()
    assert "dot_radius" not in data and "trail" not in data
    assert set(data) == {"date", "lines", "linear", "paths", "trips"}


@pytest.mark.parametrize("given", [{"dot_radius": 5}, {"dot_radius": 5.0}, {"trail": 0},
                                   {"trail": 0.0}, {"dot_radius": 5, "trail": 0}])
def test_a_value_at_its_default_writes_nothing(given):
    data = _built(**given).to_json()
    assert "dot_radius" not in data and "trail" not in data
    assert data == _built().to_json()


def test_the_defaults_are_the_pages_own_dot_and_no_trail():
    assert (animate.DOT_RADIUS, animate.TRAIL) == (5, 0)
    assert (animate.DOT_RADIUS_RANGE, animate.TRAIL_RANGE) == ((2, 12), (0, 3))


def test_each_key_is_written_alone_and_by_its_name():
    assert _built(dot_radius=8).to_json()["dot_radius"] == 8
    assert "trail" not in _built(dot_radius=8).to_json()
    assert _built(trail=1.5).to_json()["trail"] == 1.5
    assert "dot_radius" not in _built(trail=1.5).to_json()
    both = _built(dot_radius=7.5, trail=3).to_json()
    assert (both["dot_radius"], both["trail"]) == (7.5, 3)


def test_a_whole_number_is_written_as_one(tmp_path):
    graph = _graph([[("A", "0072bc")]])
    drawn = render(graph, labels=False)
    animation = animate.build(drawn, graph, [], DAY, dot_radius=8.0, trail=2.0)
    positions, page = animate.write(animation, drawn.svg, tmp_path, stem="whole")
    text = positions.read_text(encoding="utf-8")
    assert '"dot_radius":8,' in text and '"trail":2,' in text
    element = re.search(r'<script id="data" type="application/json">(.*?)</script>',
                        page.read_text(encoding="utf-8"), re.S).group(1)
    data = json.loads(element)
    assert (data["dot_radius"], data["trail"]) == (8, 2)


def test_the_page_and_the_positions_file_a_map_had_are_as_they_were(tmp_path):
    """A map built without the two numbers, or with them at their defaults,
    writes the page and the positions file it wrote before, byte for byte."""
    graph = _graph([[("A", "0072bc")], [("A", "0072bc")]])
    drawn = render(graph)
    plain = animate.build(drawn, graph, [], DAY)
    explicit = animate.build(drawn, graph, [], DAY, dot_radius=5, trail=0)
    chosen = animate.build(drawn, graph, [], DAY, dot_radius=9, trail=1)
    written = {}
    for name, animation in (("plain", plain), ("explicit", explicit), ("chosen", chosen)):
        positions, page = animate.write(animation, drawn.svg, tmp_path / name, stem="p")
        written[name] = (positions.read_bytes(), page.read_bytes())
    assert written["explicit"] == written["plain"]
    assert written["chosen"][0] != written["plain"][0]
    # What differs is the two keys and nothing else.
    without = json.loads(written["chosen"][0])
    assert without.pop("dot_radius") == 9 and without.pop("trail") == 1
    assert without == json.loads(written["plain"][0])
