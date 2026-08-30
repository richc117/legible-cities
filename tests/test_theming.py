"""The map's page furniture is themeable; its line colours never are.

An embedding page that wants a dark map reaches for `filter: invert()`, which
also inverts the agency's line colours -- LA's A Line comes out orange. The
renderer avoids that by exposing only the furniture as CSS variables.
"""

import re

import pytest

from schematic.crs import to_mercator
from schematic.linegraph import LineGraph
from schematic.pipeline import GRAPH_DIR
from schematic.render import Style, render

KEY = "la-metro-rail"

pytestmark = pytest.mark.skipif(
    not (GRAPH_DIR / KEY / "03_octi.json").exists(),
    reason="run the pipeline once to populate data/graphs",
)


@pytest.fixture(scope="module")
def graph():
    return LineGraph.from_geojson(GRAPH_DIR / KEY / "03_octi.json").reproject(to_mercator)


def line_strokes(svg: str) -> set[str]:
    return set(re.findall(r'<g class="line" data-line="[^"]+" stroke="([^"]+)"', svg))


def test_unthemed_svg_has_no_css_variables(graph):
    """A standalone SVG must render identically wherever it is opened."""
    svg = render(graph).svg
    assert "var(--map" not in svg
    assert 'fill="#ffffff"' in svg


def test_themed_svg_vars_the_furniture_with_literal_fallbacks(graph):
    svg = render(graph, style=Style(themed=True)).svg
    for name, fallback in [("bg", "#ffffff"), ("station-fill", "#ffffff"),
                           ("station-stroke", "#111111"), ("label", "#111111")]:
        assert f"var(--map-{name}, {fallback})" in svg, name


def test_line_colours_are_never_themed(graph):
    """The regression this whole mechanism exists to prevent."""
    plain = render(graph).svg
    themed = render(graph, style=Style(themed=True)).svg
    assert line_strokes(plain) == line_strokes(themed)
    assert all(c.startswith("#") for c in line_strokes(themed))
    # LA's A Line, specifically.
    assert "#0072bc" in themed.lower()


def test_geometry_is_identical_either_way(graph):
    """Theming must change colour attributes and nothing else."""
    plain = render(graph).svg
    themed = render(graph, style=Style(themed=True)).svg
    strip = lambda s: re.sub(r'var\(--map-[a-z-]+, ([^)]*)\)', r'\1', s)
    assert strip(themed) == plain


def test_render_emits_a_network_only_viewbox(graph):
    """The labels toggle needs a box that excludes the label overhang."""
    svg = render(graph, style=Style(themed=True)).svg
    full = re.search(r'viewBox="([^"]+)"', svg).group(1).split()
    tight = re.search(r'data-viewbox-nolabels="([^"]+)"', svg).group(1).split()
    full_area = float(full[2]) * float(full[3])
    tight_area = float(tight[2]) * float(tight[3])
    assert 0 < tight_area <= full_area
