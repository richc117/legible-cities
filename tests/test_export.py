"""The export presets, framing and palettes.

All pure Python: no browser, no ffmpeg. The parts that need those are exercised
by running ``bin/export`` -- what is worth asserting here is the arithmetic and
the agreement between the three places the palette is written down.
"""

import re
from pathlib import Path

import pytest

from schematic import export, feeds

PAGE = Path(export.__file__).parent / "page" / "page.html"
SITE_CSS = feeds.REPO_ROOT / "site" / "src" / "assets" / "style.css"


# ------------------------------------------------------------------- presets


def test_every_preset_is_wellformed():
    for name, p in export.PRESETS.items():
        assert p.name == name
        assert p.kind in {"still", "video", "vector"}
        assert p.fmt in {"png", "jpg", "mp4", "gif", "svg"}
        if p.kind == "vector":
            continue
        assert p.width >= 600 and p.height >= 600, name
        if p.fmt == "mp4":
            # H.264's 4:2:0 chroma subsampling cannot encode an odd dimension.
            # GIF can, which is why this is keyed on the format and not the kind.
            assert p.width % 2 == 0 and p.height % 2 == 0, name


def test_every_video_preset_names_a_real_storyboard():
    for p in export.PRESETS.values():
        if p.kind == "video":
            assert p.storyboard in export.STORYBOARDS, p.name


def test_safe_zones_only_where_the_platform_overlays_the_image():
    """Instagram draws its interface over a full-bleed portrait. A Bluesky or
    LinkedIn image sits in a card with nothing on top of it, so a "safe area"
    there is just Instagram's geometry on someone else's canvas -- and a preview
    covered in magenta guides is easy to mistake for the deliverable."""
    for p in export.PRESETS.values():
        if p.safe_zones:
            assert p.platform == "Instagram", p.name
            assert p.height > p.width, p.name          # portrait, full-bleed
    assert any(p.safe_zones for p in export.PRESETS.values())
    for name in ["bluesky", "linkedin", "x", "instagram-post"]:
        assert not export.PRESETS[name].safe_zones, name


def test_safe_preview_refuses_where_it_would_be_meaningless():
    with pytest.raises(ValueError, match="does not draw its interface"):
        export.safe_preview("la-metro-rail", export.PRESETS["bluesky"])


def test_size_limits_are_set_where_the_platform_has_one():
    # These are the two that reject an upload rather than re-encode it.
    assert export.PRESETS["bluesky"].max_bytes
    assert export.PRESETS["bluesky-video"].max_bytes
    # Bluesky's image cap is why that preset is JPEG and not PNG.
    assert export.PRESETS["bluesky"].fmt == "jpg"


# ------------------------------------------------------------------- framing


@pytest.mark.parametrize("box,aspect", [
    ((0, 0, 1400, 1000), 9 / 16),      # wide network, tall frame
    ((0, 0, 1400, 1000), 4 / 5),
    ((0, 0, 1000, 1400), 16 / 9),      # tall network, wide frame
    ((-31, -32, 1717, 1223), 1.0),     # LA's real box
    ((-31, -31, 1692, 1787), 9 / 16),  # Mexico City's real box
])
def test_padded_box_reaches_the_aspect_without_cropping(box, aspect):
    x, y, w, h = export.padded_box(box, aspect)
    assert w / h == pytest.approx(aspect, rel=1e-6)
    # Growing only: the original box must still fit inside the padded one, or
    # the export has cut off real stations.
    assert x <= box[0] + 1e-6 and y <= box[1] + 1e-6
    assert x + w >= box[0] + box[2] - 1e-6
    assert y + h >= box[1] + box[3] - 1e-6


def test_padding_is_asymmetric_so_the_gutter_is_usable():
    """The added height is where the title goes; centring it wastes the space."""
    box = (0, 0, 1400, 1000)
    x, y, w, h = export.padded_box(box, 9 / 16, frame_top=0.46)
    above, below = -y, (y + h) - 1000
    assert above != pytest.approx(below)
    assert above < below           # slightly more room under the network


def test_a_box_already_at_the_aspect_is_left_alone():
    box = (0, 0, 1080, 1920)
    assert export.padded_box(box, 1080 / 1920) == pytest.approx(box)


# ------------------------------------------------------------------ palettes


def _hexes(text: str, names) -> dict:
    out = {}
    for n in names:
        m = re.search(rf"--{n}:\s*(#[0-9a-fA-F]{{6}})", text)
        if m:
            out[n] = m.group(1).lower()
    return out


def test_palette_matches_the_animation_page():
    """Three copies of these colours exist. They have to agree, and the failure
    mode -- a dark map on a light ground -- only shows up after it is posted."""
    page = PAGE.read_text()
    dark_block = page[page.index(":root {"):page.index(':root[data-theme="sepia"]')]
    names = ["map-bg", "map-label", "map-station-fill", "map-station-stroke", "train-halo"]
    found = _hexes(dark_block, names)
    for name, value in found.items():
        key = name.replace("map-", "")
        if key in export.PALETTES["dark"]:
            # The page paints the stage as a card (--map-bg is bg-soft); an
            # export deliberately uses the page background so the padded frame
            # reads as one surface. Every other colour must match exactly.
            if key == "bg":
                continue
            assert export.PALETTES["dark"][key] == value, name


@pytest.mark.skipif(not SITE_CSS.exists(), reason="site stylesheet missing")
def test_palette_matches_the_site_stylesheet():
    css = SITE_CSS.read_text()
    root = css[css.index(":root {"):css.index(':root[data-theme="sepia"]')]
    assert export.PALETTES["dark"]["bg"] == _hexes(root, ["bg"])["bg"]


def test_resolve_replaces_variables_and_leaves_line_colours(tmp_path):
    svg = ('<rect fill="var(--map-bg, #ffffff)"/>'
           '<g class="line" data-line="A" stroke="#0072bc"/>'
           '<circle class="train" stroke="var(--train-halo, #15120f)"/>')
    out = export.resolve(svg, export.PALETTES["dark"])
    assert "var(" not in out
    assert "#0072bc" in out                       # the agency's colour, untouched
    assert export.PALETTES["dark"]["bg"] in out
    assert export.PALETTES["dark"]["train-halo"] in out


# ---------------------------------------------------------------- storyboards


def test_frame_counts_are_exact():
    for name, beats in export.STORYBOARDS.items():
        n = export.frame_count(beats, 30)
        assert n == sum(round(b.secs * 30) for b in beats), name
        assert n > 0


def test_sweeps_stay_slow_enough_to_read():
    """A whole service day stepped across ten seconds jumps ~150 simulated
    seconds a frame, and trains teleport rather than move."""
    day = (0.0, 86_400.0)
    for name, beats in export.STORYBOARDS.items():
        if name == "day":
            continue          # documented as needing a long clip, and it has one
        for b in beats:
            if b.sweep:
                assert export.sweep_rate(b, 30, day) < export.READABLE_SWEEP, \
                    f"{name} sweeps too fast"


def test_every_beat_names_a_real_view():
    for name, beats in export.STORYBOARDS.items():
        for b in beats:
            assert b.view in (None,) + export.VIEWS, name


# ------------------------------------------------------------------- registry


def test_every_feed_has_a_city_and_network():
    for key, feed in feeds.FEEDS.items():
        assert feed.city and feed.network, key


def test_no_title_repeats_its_city():
    """The reason these are separate fields: composing city + name mechanically
    gives 'Chicago - Chicago 'L'' and 'Miami - Miami Metrorail'."""
    for key, feed in feeds.FEEDS.items():
        first = feed.city.split()[0].lower()
        assert first not in feed.network.lower(), f"{key}: {feed.city} / {feed.network}"


# ------------------------------------------------------------------ alt text


@pytest.mark.parametrize("view", ["map", "linear", "time"])
def test_alt_text_is_written_for_every_view(view):
    text = export.alt_text("cdmx-metro", view, stations=168, lines=12)
    assert "Mexico City" in text
    assert len(text) > 60
    assert text[0].isupper() and text.rstrip().endswith(".")


# ----------------------------------------------------------------------- urls


def test_url_carries_the_frame_and_the_name():
    url = export.url_for("cdmx-metro", export.PRESETS["instagram-reel"])
    assert "present=1" in url
    assert "frame=1080%3A1920" in url
    assert "city=Mexico+City" in url
    assert url.startswith("file://")


def test_exports_refuse_to_write_into_the_repo(tmp_path):
    with pytest.raises(ValueError, match="repository"):
        export._guard_outside_repo(feeds.REPO_ROOT / "out" / "somewhere")
    export._guard_outside_repo(tmp_path)      # must not raise


# --------------------------------------------------------------- geographic


def test_geographic_is_a_view_the_exporter_knows():
    assert export.GEO_VIEW in export.VIEWS
    assert export.wants_geographic(view=export.GEO_VIEW)
    assert not export.wants_geographic(view="map")


def test_the_transform_storyboards_open_already_in_their_view():
    """Every other beat morphs into its view; frame 0 has to already be in one.

    Without tween=0 the clip opens on the schematic map and bends backwards into
    geography -- the argument told in reverse, and only visible on playback.
    """
    for name in ("transform", "transform-loop", "essay-loop"):
        first = export.STORYBOARDS[name][0]
        assert first.view == export.GEO_VIEW, name
        assert first.tween == 0, f"{name} would animate into its opening view"


def test_the_essay_loop_keeps_the_landing_page_figure_timing():
    """`essay-loop` exports figure two of the essay, so it has to stay figure two.

    The page runs that cycle itself, from MORPH and HOLD in present.js. Two
    copies of a number drift; this reads the page's.
    """
    js = (Path(export.__file__).parent / "page" / "present.js").read_text()
    morph = float(re.search(r"var MORPH = ([\d.]+)", js).group(1))
    hold = float(re.search(r"HOLD = ([\d.]+)", js).group(1))
    assert (export.PAGE_MORPH, export.PAGE_HOLD) == (morph, hold)

    beats = export.STORYBOARDS["essay-loop"]
    for b in beats[1:]:
        assert b.tween == morph
        assert b.secs == morph + hold
    # There and back: the page returns through the schematic map rather than
    # cutting from the rows to the ground, and ends where it began so it loops.
    assert [b.view for b in beats] == ["geographic", "map", "linear", "map",
                                       "geographic"]


def test_a_geographic_export_is_refused_where_there_is_no_geometry():
    """The page has nothing to raise -- it just shows the schematic map -- so an
    export would come out looking dull rather than broken."""
    geo_feeds = [k for k, f in feeds.FEEDS.items() if f.geographic]
    plain = [k for k, f in feeds.FEEDS.items() if not f.geographic]
    assert geo_feeds, "no feed carries geographic geometry any more"

    export.check_geographic(geo_feeds[0], storyboard="transform")   # allowed
    with pytest.raises(ValueError, match="geographic"):
        export.check_geographic(plain[0], storyboard="transform")
    with pytest.raises(ValueError, match="geographic"):
        export.check_geographic(plain[0], view=export.GEO_VIEW)
    # A storyboard that never asks for it is fine on any feed.
    export.check_geographic(plain[0], storyboard="tour")


def test_a_storyboard_is_described_by_the_views_it_visits():
    """A four-view clip labelled "schematic map of..." is simply wrong."""
    assert export.storyboard_views("transform") == "geographic -> map -> linear -> time"
    alt = export.storyboard_alt("la-metro-rail", "transform", stations=110, lines=6)
    for phrase in ("where its track actually runs", "45-degree grid", "own row",
                   "time chart"):
        assert phrase in alt
    # One view is not a sequence; fall back to the plain description.
    assert export.storyboard_alt("la-metro-rail", "run") == export.alt_text(
        "la-metro-rail", "map")
