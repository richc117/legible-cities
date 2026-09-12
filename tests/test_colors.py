"""Line colours are inputs: a caller's override, else the feed's own, else a default.

The resolution lives in ``render.line_colors`` and happens once per build.
The map's strokes and the page's ``data.lines`` read the same dictionary,
so an uncoloured line looks the same on the map, in the chips, under the
train dots and in the time chart. Pittsburgh's inclines (``MI``, ``DQI``)
are the case that shaped this: no ``route_color`` in the feed, and before
E06 the page had no entry for them and each element fell back on its own.
"""

import datetime as dt
import hashlib
import json
import re
from pathlib import Path

import pytest

from schematic import animate, pipeline
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph
from schematic.render import Style, check_color, line_colors, render

SNAPSHOTS = Path(__file__).parent / "fixtures" / "colors"
DEFAULT = Style().default_line_color
DAY = dt.date(2026, 9, 10)


def _graph(lines_by_edge: list[list[tuple[str, str | None]]]) -> LineGraph:
    """A chain of stations, one edge per entry, each carrying the lines
    given as (label, route_color as GTFS writes it, or None)."""
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [i * 10.0, 0.0]},
              "properties": {"id": f"n{i}", "station_id": f"S{i}", "station_label": f"Stop {i}"}}
             for i in range(len(lines_by_edge) + 1)]
    for i, lines in enumerate(lines_by_edge):
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [[i * 10.0, 0.0], [(i + 1) * 10.0, 0.0]]},
                      "properties": {"from": f"n{i}", "to": f"n{i + 1}",
                                     "lines": [{"id": label, "label": label,
                                                **({"color": color} if color else {})}
                                               for label, color in lines]}})
    return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})


def strokes(svg: str) -> dict[str, str]:
    return dict(re.findall(r'<g class="line" data-line="([^"]+)" stroke="([^"]+)"', svg))


# ------------------------------------------------------------- resolution

def test_an_override_wins_then_the_feed_then_the_default():
    graph = _graph([[("A", "0072bc"), ("B", None)], [("C", "#ff0000"), ("B", "")]])
    assert line_colors(graph, default=DEFAULT) == {"A": "#0072bc", "B": DEFAULT, "C": "#ff0000"}
    resolved = line_colors(graph, default="#123456",
                           overrides={"A": "#ABCDEF", "Z": "#000000"})
    # Keyed in the order the lines first appear; an override for a line the
    # graph does not carry is ignored, not refused.
    assert list(resolved) == ["A", "B", "C"]
    assert resolved == {"A": "#ABCDEF", "B": "#123456", "C": "#ff0000"}


@pytest.mark.parametrize("bad", ["red", "123456", "#12345", "#1234567", "#ggg000", "", 0x123456,
                                 None, ["#123456"]])
def test_a_colour_not_written_rrggbb_is_refused(bad):
    graph = _graph([[("A", None)]])
    with pytest.raises(ValueError, match="'A' must be a colour written #rrggbb"):
        line_colors(graph, default=DEFAULT, overrides={"A": bad})
    with pytest.raises(ValueError, match="default_color must be a colour written #rrggbb"):
        check_color(bad, "default_color")


def test_the_map_and_the_page_read_one_resolution():
    """Every line has a stroke and a page entry, and they are the same string."""
    graph = _graph([[("A", "0072bc"), ("B", None)], [("B", None), ("C", "ff0000")]])
    r = render(graph, colors={"C": "#00ff00"}, labels=False)
    assert strokes(r.svg) == {"A": "#0072bc", "B": DEFAULT, "C": "#00ff00"} == r.colors
    page = animate.build(r, graph, [], DAY)
    assert page.lines == r.colors
    assert page.to_json()["lines"] == r.colors


# --------------------------------------------------- over stored layouts

def _stored(key: str) -> pipeline.Layout:
    found = pipeline.stored(key)
    if found is None:
        pytest.skip(f"needs the stored layout for {key}")
    return found


def test_an_override_changes_that_lines_stroke_and_entry_and_nothing_else():
    graph = LineGraph.from_geojson(_stored("la-metro-rail").paths["octi"]).reproject(to_mercator)
    plain = render(graph, style=Style(themed=True))
    changed = render(graph, style=Style(themed=True), colors={"A": "#123456"})
    assert strokes(plain.svg)["A"] == "#0072bc"
    assert strokes(changed.svg) == {**strokes(plain.svg), "A": "#123456"}
    # The one attribute is the whole difference between the two documents.
    assert changed.svg.replace('stroke="#123456"', 'stroke="#0072bc"') == plain.svg
    assert animate.build(changed, graph, [], DAY).lines["A"] == "#123456"


def test_uncoloured_lines_get_the_default_on_the_map_and_in_the_page(tmp_path):
    """Pittsburgh's inclines carry no route_color: the default reaches their
    strokes and their page entries, and a chosen default replaces it in both;
    an override alongside it lands on the line it names."""
    stored = _stored("pittsburgh-t")
    result = pipeline.run("pittsburgh-t", layout=stored.id, date=DAY, out_dir=tmp_path)
    drawn = strokes(result.render.svg)
    assert drawn["MI"] == drawn["DQI"] == DEFAULT
    assert result.animation.lines["MI"] == result.animation.lines["DQI"] == DEFAULT
    assert set(result.animation.lines) == set(drawn) == {"RED", "SLVR", "BLUE", "MI", "DQI"}

    chosen = pipeline.run("pittsburgh-t", layout=stored.id, date=DAY, out_dir=tmp_path,
                          default_color="#123456", colors={"RED": "#000000"})
    drawn = strokes((tmp_path / "pittsburgh-t.svg").read_text())
    assert drawn["MI"] == drawn["DQI"] == "#123456" and drawn["RED"] == "#000000"
    written = json.loads((tmp_path / "pittsburgh-t.positions.json").read_text())["lines"]
    assert written == chosen.animation.lines == {**drawn}
    assert "#888888" not in (tmp_path / "pittsburgh-t.html").read_text()


def test_a_bad_colour_is_refused_before_anything_is_written(tmp_path):
    stored = _stored("pittsburgh-t")
    with pytest.raises(ValueError, match="default_color must be a colour written #rrggbb"):
        pipeline.run("pittsburgh-t", layout=stored.id, date=DAY, out_dir=tmp_path,
                     default_color="123456")
    assert not list(tmp_path.iterdir())


def test_without_overrides_the_page_changes_only_by_the_missing_entries(tmp_path):
    """The snapshot was taken from the same stored layout before E06: the SVG,
    the page around its data and the data apart from ``lines`` hash the same,
    and ``lines`` gains exactly the two lines the feed leaves uncoloured. Over
    the stored stage graph, not a regeneration, because a regeneration also
    moves geometry (app ADR-023). ``page_without_data`` is re-pinned whenever
    ``page.html`` changes on purpose -- E24 rebuilt the line chips out of DOM
    nodes -- and the other three hashes are the ones that must not move."""
    snapshot = json.loads((SNAPSHOTS / "pittsburgh-t.json").read_text())
    stored = _stored("pittsburgh-t")
    if stored.id != snapshot["layout"]:
        pytest.skip("the stored Pittsburgh layout is not the one the snapshot was taken from")
    result = pipeline.run("pittsburgh-t", layout=stored.id, date=dt.date.fromisoformat(snapshot["date"]),
                          out_dir=tmp_path)
    sha = lambda text: hashlib.sha256(text.encode()).hexdigest()
    data = json.loads((tmp_path / "pittsburgh-t.positions.json").read_text())
    html = (tmp_path / "pittsburgh-t.html").read_text()
    assert sha(result.render.svg) == snapshot["svg"]
    assert sha(html.replace(animate._json_for_script(data), "")) == snapshot["page_without_data"]
    rest = {k: v for k, v in data.items() if k != "lines"}
    assert sha(json.dumps(rest, sort_keys=True, separators=(",", ":"))) == snapshot["data_without_lines"]
    assert data["lines"] == {**snapshot["lines"], "MI": DEFAULT, "DQI": DEFAULT}
