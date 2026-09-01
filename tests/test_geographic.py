"""The geographic axis: pairing the pre-octilinear graph to the drawn one.

The morph lerps vertex i of a drawn track against vertex i of its geographic
twin, so everything here is really one property -- that the two agree on how
many vertices there are and which end is which. Get either wrong and the map
does not fail loudly; it turns an edge inside out halfway through a transition.
"""

import json

import pytest

from schematic import animate, pipeline
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph
from schematic.render import Style, render, resample


def test_resample_hits_both_ends_and_the_asked_for_count():
    line = [(0.0, 0.0), (10.0, 0.0)]
    out = resample(line, 5)
    assert len(out) == 5
    assert out[0] == pytest.approx((0.0, 0.0))
    assert out[-1] == pytest.approx((10.0, 0.0))
    # Evenly by arc length, which is the only spacing both shapes can agree on.
    assert out[2] == pytest.approx((5.0, 0.0))


def test_resample_spaces_by_length_not_by_vertex():
    # A short first segment and a long second one: by vertex the midpoint would
    # land on the corner, by length it lands well down the long leg.
    bent = [(0.0, 0.0), (1.0, 0.0), (11.0, 0.0)]
    assert resample(bent, 3)[1] == pytest.approx((5.5, 0.0))


def test_resample_survives_a_degenerate_polyline():
    assert resample([(3.0, 4.0)], 4) == [(3.0, 4.0)] * 4
    assert resample([(3.0, 4.0), (3.0, 4.0)], 3) == [(3.0, 4.0)] * 3


def _graph(coords: dict[str, tuple[float, float]], edges) -> LineGraph:
    """A two-feature GeoJSON graph, written the way LOOM writes one."""
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": list(c)},
              "properties": {"id": nid, "station_id": nid.upper()}}
             for nid, c in coords.items()]
    for src, dst, geom in edges:
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString",
                                   "coordinates": [list(p) for p in geom]},
                      "properties": {"from": src, "to": dst,
                                     "lines": [{"id": "L", "label": "L"}]}})
    return LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})


def test_pairing_keys_on_station_id_not_node_id():
    # Node ids are process pointers and never survive a LOOM stage, so the two
    # graphs here deliberately share no id at all.
    drawn = _graph({"a": (0.0, 0.0), "b": (10.0, 0.0)},
                   [("a", "b", [(0.0, 0.0), (10.0, 0.0)])])
    geo = _graph({"x": (0.0, 0.0), "y": (6.0, 4.0)},
                 [("x", "y", [(0.0, 0.0), (3.0, 4.0), (6.0, 4.0)])])
    # ...but the same stations. _graph derives station_id from the node id, so
    # line them up by hand the way the real stages do.
    for node, sid in (("x", "A"), ("y", "B")):
        geo.nodes[node].station_id = sid

    r = render(drawn, width=100.0, style=Style())
    layer = animate.geographic_tracks(geo, drawn, r)

    assert layer, "no geographic geometry was paired at all"
    for eid, pts in layer.tracks.items():
        drawn_track = next(t for t in r.tracks.values() if t.element_id == eid)
        assert len(pts) == len(drawn_track.points), "vertex counts must match to lerp"


def test_pairing_is_oriented_the_same_way_as_the_drawn_track():
    """The failure this guards is silent: an edge that morphs inside out."""
    drawn = _graph({"a": (0.0, 0.0), "b": (10.0, 0.0)},
                   [("a", "b", [(0.0, 0.0), (10.0, 0.0)])])
    # The same edge, emitted in the opposite direction.
    geo = _graph({"x": (0.0, 0.0), "y": (10.0, 6.0)},
                 [("y", "x", [(10.0, 6.0), (0.0, 0.0)])])
    for node, sid in (("x", "A"), ("y", "B")):
        geo.nodes[node].station_id = sid

    r = render(drawn, width=100.0, style=Style())
    layer = animate.geographic_tracks(geo, drawn, r)
    eid, pts = next(iter(layer.tracks.items()))
    track = next(t for t in r.tracks.values() if t.element_id == eid)

    # Compare against the geographic *station* positions rather than the drawn
    # track's own ends: the drawn ends are symmetric about the box the fit
    # centres everything in, so they cannot tell the two apart.
    def near(a, b):
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

    src_xy, dst_xy = layer.nodes[track.src], layer.nodes[track.dst]
    assert near(pts[0], src_xy) < near(pts[0], dst_xy), "the geographic twin runs backwards"
    assert near(pts[-1], dst_xy) < near(pts[-1], src_xy)


@pytest.mark.skipif(not (pipeline.GRAPH_DIR / "la-metro-rail" / "03_octi.json").exists(),
                    reason="needs a built graph in data/")
def test_los_angeles_pairs_every_drawn_track():
    """LA is the one the essay animates, so a gap there is a visible hole.

    A track with no twin holds still while the network around it moves, which
    reads as the map tearing rather than as a missing feature.
    """
    d = pipeline.GRAPH_DIR / "la-metro-rail"
    drawn = LineGraph.from_geojson(d / "03_octi.json").reproject(to_mercator)
    geo = LineGraph.from_geojson(d / "02_loom.json").reproject(to_mercator)
    r = render(drawn, width=1600.0, style=Style(themed=True))
    layer = animate.geographic_tracks(geo, drawn, r)

    assert len(layer.tracks) == len(r.tracks)
    assert len(layer.nodes) == len(drawn.stations)
    for track in r.tracks.values():
        assert len(layer.tracks[track.element_id]) == len(track.points)


@pytest.mark.skipif(not (pipeline.GRAPH_DIR / "la-metro-rail" / "03_octi.json").exists(),
                    reason="needs a built graph in data/")
def test_only_the_opted_in_city_carries_the_second_geometry():
    """It is a whole second copy of the network; most pages have no use for one."""
    from schematic import feeds
    assert feeds.FEEDS["la-metro-rail"].geographic
    assert not feeds.FEEDS["nyc-subway"].geographic

    la = pipeline.SITE_MAPS if hasattr(pipeline, "SITE_MAPS") else None
    del la  # the built pages are checked below, not through the pipeline

    built = pipeline.feeds.REPO_ROOT / "site" / "src" / "maps"
    if not (built / "nyc-subway.html").exists():
        pytest.skip("site not built")
    assert '"geo"' in (built / "la-metro-rail.html").read_text()
    assert '"geo"' not in (built / "nyc-subway.html").read_text()
