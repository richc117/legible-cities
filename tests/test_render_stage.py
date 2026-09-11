"""render.stage: a stored stage graph, drawn, with its counts (E15).

Gated on the checkout's cached Los Angeles layout, as test_cdmx is on its
own; the missing-stage case needs nothing stored.
"""

import pytest

from schematic import config, pipeline, render
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph

KEY = "la-metro-rail"

needs_la = pytest.mark.skipif(pipeline.stage_path(KEY, "octi") is None,
                              reason="run the pipeline once to populate data/graphs")


@needs_la
@pytest.mark.parametrize("stage", ["gtfs2graph", "loom", "octi"])
def test_a_stage_renders_with_the_counts_its_graph_has(stage):
    svg, counts = render.stage(KEY, stage)
    assert svg.startswith(("<svg", "<?xml"))
    graph = LineGraph.from_geojson(pipeline.stage_path(KEY, stage)).reproject(to_mercator)
    assert counts["nodes"] == len(graph.nodes)
    assert counts["stations"] == len(graph.stations)
    assert counts["edges"] == len(graph.edges)
    assert sorted(counts["lines"]) == sorted(graph.labels)
    assert counts["width"] > 0 and counts["height"] > 0
    if stage == "octi":
        assert counts["octilinear"] > 0.9
    else:
        assert "octilinear" not in counts


@needs_la
def test_by_layout_id_and_by_registry_entry_agree():
    stored = pipeline.stored(KEY)
    svg_a, counts_a = render.stage(KEY, "gtfs2graph", layout=stored.id, width=600)
    svg_b, counts_b = render.stage(KEY, "gtfs2graph", width=600)
    assert counts_a == counts_b
    assert svg_a == svg_b


def test_a_missing_stage_says_what_builds_it(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV, str(tmp_path))
    with pytest.raises(pipeline.LayoutMissing, match="no stored octi graph; lay the feed out first"):
        render.stage(KEY, "octi")
    with pytest.raises(pipeline.LayoutMissing, match="under layout 00000000"):
        render.stage(KEY, "topo", layout="0" * 64)
    with pytest.raises(ValueError, match="is not a stage"):
        render.stage(KEY, "labels")
