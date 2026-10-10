"""The export presets, framing and palettes.

All pure Python: no browser, no ffmpeg. The parts that need those are exercised
by running ``bin/export`` -- what is worth asserting here is the arithmetic and
the agreement between the three places the palette is written down.
"""

import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest
from pylsp_jsonrpc.exceptions import JsonRpcInvalidParams

from schematic import config, export, feeds, serve
from schematic.schedule import service_day_text
from test_serve import check, invalid

PAGE = Path(export.__file__).parent / "page" / "page.html"
SITE_CSS = config.REPO_ROOT / "site" / "src" / "assets" / "style.css"


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
    # The share card: LA's unlabelled box at the 1.91:1 every unfurler wants.
    ((-31, -31, 1162, 786.39), 1200 / 630),
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


@pytest.mark.parametrize("view", ["geographic", "map", "linear", "time"])
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


def test_no_export_url_ever_asks_for_the_controls():
    """A control visible in presentation mode is captured into the deliverable.

    bin/_record.js element-screenshots #stage, which picks up anything painted
    over that box -- that is how the title and clock get into an export. So a
    switcher would be baked into every reel, still and GIF, and worse, it would
    not sit still: the frame loop calls setView/setGeo per beat and syncButtons
    moves aria-pressed on each, so it would flicker through its pressed states
    on the beat, inside the footage. Nobody would catch that until review.

    `controls` defaults off in present.js and url_for must never set it. The day
    this fails is the day a toolbar starts appearing in the social assets.
    """
    for name, preset in export.PRESETS.items():
        for view in export.VIEWS:
            url = export.url_for("la-metro-rail", preset, view=view)
            assert "controls" not in url, f"{name}/{view}"
    # Including the safe-area preview, which is a still like any other.
    assert "controls" not in export.url_for(
        "la-metro-rail", export.PRESETS["instagram-reel"], safe=True)


def test_exports_refuse_to_write_into_the_repo(tmp_path):
    with pytest.raises(ValueError, match="repository"):
        export._guard_outside_repo(config.REPO_ROOT / "out" / "somewhere")
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

    The page runs that cycle itself, from MORPH and HOLD in present.js, and the
    essay's opening figure cycles on the same beat from embed.js. Copies of a
    number drift; this reads both of the pages'.
    """
    js = (Path(export.__file__).parent / "page" / "present.js").read_text()
    morph = float(re.search(r"var MORPH = ([\d.]+)", js).group(1))
    hold = float(re.search(r"HOLD = ([\d.]+)", js).group(1))
    assert (export.PAGE_MORPH, export.PAGE_HOLD) == (morph, hold)

    # The site's switcher cycles figure one on the same beat.
    embed = (config.REPO_ROOT / "site" / "src" / "assets" / "embed.js").read_text()
    assert re.search(r"var MORPH = ([\d.]+), HOLD = ([\d.]+);", embed).groups() \
        == (str(morph), str(hold))

    # A click on the running map's toolbar runs the same S-curve but shorter:
    # a figure morphing on a loop is content, a click is a reader waiting. It
    # must never be the slower of the two.
    page = PAGE.read_text()
    click = float(re.search(r"const CLICK_MORPH = ([\d.]+);", page).group(1))
    assert 0.6 <= click <= morph, click

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
    assert geo_feeds, "no feed carries geographic geometry any more"
    export.check_geographic(geo_feeds[0], storyboard="transform")   # allowed

    # Every registered feed carries the geometry now, so the refusal has to be
    # exercised against a feed opted out by hand -- which is the only way it can
    # arise, and exactly what the flag is still for.
    plain = "opted-out"
    real = feeds.FEEDS[geo_feeds[0]]
    monkeyed = dict(feeds.FEEDS)
    monkeyed[plain] = replace(real, key=plain, geographic=False)
    with mock.patch.dict(feeds.FEEDS, monkeyed, clear=True):
        with pytest.raises(ValueError, match="geographic"):
            export.check_geographic(plain, storyboard="transform")
        with pytest.raises(ValueError, match="geographic"):
            export.check_geographic(plain, view=export.GEO_VIEW)
        # A storyboard that never asks for it is fine on any feed.
        export.check_geographic(plain, storyboard="tour")


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


# --------------------------------------------------------------- the halves


def test_plan_is_pure_and_knows_no_recorder(tmp_path, monkeypatch):
    """`plan` reads and never writes: the desktop app asks for one over the
    protocol and captures for itself, so a plan that touched a file or
    started a browser would be doing the app's work in the wrong process."""
    import inspect
    home = tmp_path / "home"
    home.mkdir()
    home.chmod(0o500)
    monkeypatch.setenv(config.ENV, str(home))
    monkeypatch.chdir(tmp_path)
    try:
        job = export.plan("la-metro-rail", "instagram-reel", quality="draft", tag="t")
    finally:
        home.chmod(0o700)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["home"]
    assert list(home.iterdir()) == []
    assert job.mode == "video" and job.format == "mp4"
    assert (job.width, job.height, job.fps) == (1080, 1920, 30)
    assert (job.scale, job.keep, job.crf) == (1, True, 26)
    assert job.filename == "la-metro-rail-instagram-reel-t.mp4"
    assert job.beats == tuple(export.beat_payload(export.STORYBOARDS["tour"]))
    assert job.settle == export.SETTLE_MS
    for fn in (export.plan, export.encode, export.CaptureJob.recorder_job):
        source = inspect.getsource(fn)
        assert "RECORDER" not in source and "_run_recorder" not in source, fn.__name__
    flat = json.dumps(job.to_dict())
    assert "_record" not in flat


def test_a_still_is_pinned_to_the_pages_clock_and_the_two_agree():
    """A still with no `at` is taken where the page's clock starts, and the
    recorder seeks there after stopping the clock; a video's beats seek for
    themselves."""
    page = PAGE.read_text()
    start = int(re.search(r"let now = (\d+) \* 3600", page).group(1))
    assert export.PAGE_START == f"{start:02d}:00"
    still = export.plan("la-metro-rail", "instagram-post")
    assert still.at == start * 3600
    assert still.recorder_job(out=Path("/x.png"))["at"] == start * 3600
    assert export.plan("la-metro-rail", "instagram-post", at="09:30").at == 9 * 3600 + 30 * 60
    assert export.plan("la-metro-rail", "instagram-reel").at is None


def test_a_still_plans_a_still_and_a_vector_is_refused():
    job = export.plan("cdmx-metro", "instagram-post", theme="light")
    assert job.mode == "still" and job.beats == () and job.storyboard == ""
    assert job.filename == "cdmx-metro-instagram-post-light.png"
    rec = job.recorder_job(out=Path("/somewhere/x.png"))
    assert rec["mode"] == "still" and rec["out"] == "/somewhere/x.png" and "beats" not in rec
    with pytest.raises(ValueError, match="vector"):
        export.plan("cdmx-metro", "portfolio-svg")


def test_plan_takes_the_apps_page_address():
    """The desktop app serves a project's page on its own origin."""
    job = export.plan("la-metro-rail", "instagram-reel",
                      page="app://local/projects/abc/la-metro-rail.html")
    assert job.url.startswith("app://local/projects/abc/la-metro-rail.html?present=1")
    assert "file://" not in job.url


def test_plan_carries_the_sweep_note_rather_than_printing_it():
    slow = export.plan("la-metro-rail", "portfolio-mp4", storyboard="transform")
    assert slow.notes == ()
    fast = export.plan("la-metro-rail", "portfolio-mp4", storyboard="day")
    assert fast.notes and "simulated seconds per frame" in fast.notes[0]


def test_ffmpeg_is_resolved_from_the_environment(monkeypatch):
    monkeypatch.delenv("SCHEMATIC_FFMPEG", raising=False)
    assert (export.ffmpeg_path(), export.ffprobe_path()) == ("ffmpeg", "ffprobe")
    monkeypatch.setenv("SCHEMATIC_FFMPEG", "/bundle/bin/ffmpeg")
    assert export.ffmpeg_path() == "/bundle/bin/ffmpeg"
    assert export.ffprobe_path() == "/bundle/bin/ffprobe"
    monkeypatch.setenv("SCHEMATIC_FFMPEG", "/bundle/bin/ffmpeg.exe")
    assert export.ffprobe_path() == "/bundle/bin/ffprobe.exe"
    # A wrapper that is not named for ffmpeg gets ffprobe from PATH.
    monkeypatch.setenv("SCHEMATIC_FFMPEG", "/tmp/log-and-exec.sh")
    assert export.ffprobe_path() == "ffprobe"


needs_ffmpeg = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg on PATH")


def _frames(into: Path, n: int = 12, size: int = 64) -> Path:
    """A square crossing the frame: enough content that two encodes have
    something to disagree about."""
    from PIL import Image, ImageDraw
    into.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        im = Image.new("RGB", (size, size), "#15120f")
        x = int(i / max(n - 1, 1) * (size - 12))
        ImageDraw.Draw(im).rectangle([x, size // 2 - 6, x + 12, size // 2 + 6], fill="#e8b04a")
        im.save(into / f"{i:06d}.png")
    return into


def _decoded(video: Path, into: Path) -> list:
    from PIL import Image
    into.mkdir()
    subprocess.run([shutil.which("ffmpeg"), "-y", "-loglevel", "error", "-i", str(video),
                    str(into / "%06d.png")], check=True)
    return [Image.open(p).convert("RGB") for p in sorted(into.glob("*.png"))]


@needs_ffmpeg
def test_encode_reproduces_the_video_from_a_saved_frames_directory(tmp_path):
    from PIL import ImageChops
    frames = _frames(tmp_path / "frames")
    before = sorted(p.name for p in frames.iterdir())
    job = export.plan("la-metro-rail", "linkedin-video", quality="high")   # keep: no resample
    a = export.encode(job, frames, tmp_path / "a" / job.filename)[0]
    b = export.encode(job, frames, tmp_path / "b" / job.filename)[0]
    assert sorted(p.name for p in frames.iterdir()) == before, "the frames are the caller's"
    assert a.with_suffix(".mp4.json").exists()
    da, db = _decoded(a, tmp_path / "da"), _decoded(b, tmp_path / "db")
    assert len(da) == len(db) == 12
    for x, y in zip(da, db):
        worst = max(band[1] for band in ImageChops.difference(x, y).getextrema())
        assert worst <= 8


@needs_ffmpeg
@pytest.mark.skipif(os.name != "posix", reason="a shell wrapper")
def test_every_ffmpeg_call_goes_through_the_resolver(tmp_path, monkeypatch):
    """With SCHEMATIC_FFMPEG pointing at a script that logs and execs, an
    export's every ffmpeg call appears in the log: a bundled ffmpeg reaches
    them all, and nothing falls back to PATH behind its back."""
    log = tmp_path / "calls.log"
    wrapper = tmp_path / "ffmpeg"
    wrapper.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec "{shutil.which("ffmpeg")}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv("SCHEMATIC_FFMPEG", str(wrapper))
    frames = _frames(tmp_path / "frames")
    gif = export.plan("la-metro-rail", "linkedin-gif")
    export.encode(gif, frames, tmp_path / "out" / gif.filename)
    still = export.plan("la-metro-rail", "bluesky")            # standard: resampled, jpg
    big = tmp_path / "big.png"
    from PIL import Image
    Image.new("RGB", (400, 400), "#15120f").save(big)
    export.encode(still, big, tmp_path / "out" / still.filename)
    kept = export.plan("la-metro-rail", "bluesky", quality="draft")   # keep: a PNG transcoded
    export.encode(kept, big, tmp_path / "out" / "kept.jpg")
    calls = log.read_text().splitlines()
    assert len(calls) == 4, calls          # palettegen, paletteuse, the resample, the transcode
    assert all("-loglevel error" in c for c in calls)


# ------------------------------------------------ a kept still, in whose format

MAGIC = {"png": b"\x89PNG\r\n\x1a\n", "jpg": b"\xff\xd8"}      # a file's first bytes
PIL_NAME = {"png": "PNG", "jpg": "JPEG"}


def _still(path: Path, fmt: str, size: tuple[int, int] = (640, 480), *,
           noise: bool = False) -> Path:
    """A captured still in ``fmt``, whatever ``path`` is called. ``noise`` is
    the worst case for a codec: a PNG of it is far over Bluesky's limit."""
    from PIL import Image, ImageDraw
    if noise:
        im = Image.frombytes("RGB", size, random.Random(30).randbytes(size[0] * size[1] * 3))
    else:
        im = Image.new("RGB", size, "#15120f")
        ImageDraw.Draw(im).rectangle([40, 40, size[0] - 40, size[1] - 40],
                                     outline="#e8b04a", width=1)
    im.save(path, format=PIL_NAME[fmt])
    return path


@needs_ffmpeg
@pytest.mark.parametrize("quality", ["draft", "high"])      # the two that keep
@pytest.mark.parametrize("captured,preset_name,delivered", [
    ("png", "bluesky", "jpg"),              # issue 30: a PNG capture for the JPEG preset
    ("jpg", "instagram-square", "png"),     # and a JPEG for a PNG preset
    ("png", "instagram-square", "png"),     # the formats agree: a copy
    ("jpg", "bluesky", "jpg"),              # agree, as the engine's own recorder writes it
])
def test_a_kept_still_is_in_the_presets_format_at_the_size_it_was_captured(
        tmp_path, quality, captured, preset_name, delivered):
    from PIL import Image
    preset = export.PRESETS[preset_name]
    assert preset.fmt == delivered
    job = export.plan("la-metro-rail", preset_name, quality=quality)
    assert job.keep
    source = _still(tmp_path / f"capture.{captured}", captured)
    before = source.read_bytes()
    dest = tmp_path / "out" / job.filename
    assert export.encode(job, source, dest) == [dest]
    data = dest.read_bytes()
    assert data.startswith(MAGIC[delivered]), data[:8].hex()
    with Image.open(dest) as im:
        assert im.format == PIL_NAME[delivered]
        # Not the preset's 1200x900 or 1080x1080: a kept capture is not resampled.
        assert im.size == (640, 480) != (preset.width, preset.height)
    if captured == delivered:
        assert data == before, "the formats agree, so it is the capture itself"
    export.check_size(dest, preset)
    assert export.sidecar_path(dest).exists()
    assert source.read_bytes() == before, "the source is the caller's"


@needs_ffmpeg
def test_a_dense_png_capture_for_the_jpeg_preset_is_transcoded_under_its_limit(tmp_path):
    """The case that made it matter: a dense map is well over Bluesky's limit
    as a PNG, so the copy was refused by ``check_size``; as the JPEG the
    preset is, it is not."""
    preset = export.PRESETS["bluesky"]
    job = export.plan("la-metro-rail", "bluesky", quality="high")
    # 740 by 560 of noise: the PNG is 28% over the limit and ffmpeg's JPEG of it
    # at -q:v 3 is 30% under, so neither side of the test rests on one
    # encoder's byte count (800 by 600 left the JPEG 19% under).
    source = _still(tmp_path / "capture.png", "png", (740, 560), noise=True)
    assert source.stat().st_size > preset.max_bytes
    dest = tmp_path / "out" / job.filename
    export.encode(job, source, dest)
    assert dest.read_bytes().startswith(MAGIC["jpg"])
    assert dest.stat().st_size <= preset.max_bytes
    export.check_size(dest, preset)


@needs_ffmpeg
def test_a_stills_bytes_decide_its_format_not_its_name(tmp_path):
    """A client's name for what it captured is a label, and a mislabelled
    file is the defect: PNG bytes called ``.jpg`` still go to the JPEG preset
    as a transcode, and JPEG bytes called ``.png`` to a PNG preset likewise."""
    bluesky = export.plan("la-metro-rail", "bluesky", quality="draft")
    liar = _still(tmp_path / "capture.jpg", "png")
    out = export.encode(bluesky, liar, tmp_path / "a" / bluesky.filename)[0]
    assert out.read_bytes().startswith(MAGIC["jpg"])
    square = export.plan("la-metro-rail", "instagram-square", quality="draft")
    liar = _still(tmp_path / "capture.png", "jpg")
    out = export.encode(square, liar, tmp_path / "b" / square.filename)[0]
    assert out.read_bytes().startswith(MAGIC["png"])


@needs_ffmpeg
def test_a_kept_still_that_is_no_image_leaves_nothing_behind(tmp_path):
    """Copied as it was, it would have been delivered under an image's name
    with a sidecar vouching for it. Transcoded, ffmpeg refuses it, and an
    encode that fails leaves neither file."""
    job = export.plan("la-metro-rail", "bluesky", quality="draft")
    junk = tmp_path / "capture.png"
    junk.write_bytes(b"not a picture")
    dest = tmp_path / "out" / job.filename
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        export.encode(job, junk, dest)
    assert not dest.exists() and not export.sidecar_path(dest).exists()
    assert junk.read_bytes() == b"not a picture"


def test_the_sidecar_names_a_persons_source_without_its_secrets(tmp_path, monkeypatch):
    """The file beside an export travels with the picture, and a feed a person
    added can come from a keyed link (issue 32). The record keeps the address
    whole; the sidecar says where the feed came from and nothing else."""
    keyed = "https://someone:pw@example.test/feeds/gtfs.zip?api_key=S3CRET#tok"
    mine = feeds.Feed(key="keyed", name="Keyed", url=keyed, city="Springfield",
                      network="Transit", source="user")
    monkeypatch.setattr(feeds, "get", lambda key: mine)
    written = tmp_path / "keyed.png"
    written.write_bytes(b"png")
    export._write_sidecar("keyed", export.PRESETS["bluesky"], [written],
                          theme="dark", view="schematic")
    text = export.sidecar_path(written).read_text(encoding="utf-8")
    assert json.loads(text)["source"] == (
        "https://<redacted>@example.test/feeds/gtfs.zip?api_key=<redacted>#<redacted>")
    for secret in ("S3CRET", "someone", "pw@", "tok"):
        assert secret not in text
    assert mine.url == keyed


def test_the_sidecar_names_a_presets_source_as_the_registry_has_it(tmp_path):
    """A preset's address is public and is the credit the feed is owed. One
    of them carries a query (Mexico City's ``?alt=media``), which a redaction
    applied to every feed alike would have turned into a marker."""
    assert any("?" in feed.url for feed in feeds.FEEDS.values())
    for key, feed in feeds.FEEDS.items():
        assert feed.shown_url == feed.url, key
    written = tmp_path / "cdmx.png"
    written.write_bytes(b"png")
    export._write_sidecar("cdmx-metro", export.PRESETS["bluesky"], [written],
                          theme="dark", view="schematic")
    meta = json.loads(export.sidecar_path(written).read_text(encoding="utf-8"))
    assert meta["source"] == feeds.FEEDS["cdmx-metro"].url


# ------------------------------------------------- a title card and a draw-in
#
# Issue 44, as settled on 9 Oct 2026: a beat takes `card` and `draw_in`,
# booleans false when left out. A card lasts a second at least and a draw-in
# two; a list draws in once, on the geographic or the map view, with no row or
# chart before it and no sweep in it, and every refusal names the beat and the
# field. The flags are written only where true, so nothing a list without them
# plans moves (tests/test_beats.py pins the eight names' plans), and a card puts
# the city, the network and the day on the address whether or not the title is
# on, with a note when its words need longer to read than its beat lasts.

KEY = "la-metro-rail"
APP_PAGE = "app://local/projects/p1/la-metro-rail.html"
DATE = "2026-09-05"
CAPTION = "Trains every few minutes from first light, and long gaps between them after ten."
OPENING = {"secs": 2, "view": "map", "at": "07:00"}
# The issue's own list: a card, the network drawing in on the map, the morning.
CARDED = [{**OPENING, "speed": 0, "card": True}, {"secs": 6, "draw_in": True},
          {"secs": 10, "sweep": True, "hours": 2}]


def _url_query(job: export.CaptureJob) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(job.url).query).items()}


def test_a_beat_takes_a_card_and_a_draw_in_false_when_left_out():
    beats = export.authored_beats(CARDED)
    assert [(b.card, b.draw_in) for b in beats] == [(True, False), (False, True), (False, False)]
    assert (export.Beat(1).card, export.Beat(1).draw_in) == (False, False)
    assert {"card", "draw_in"} <= set(export.BEAT_FIELDS)
    # Given as Beats, checked the same.
    assert export.authored_beats(beats) == beats


def test_the_flags_are_written_only_where_they_are_true():
    """A beat's payload and export.storyboards' row carry `card` and `draw_in`
    only where true, so a beat without them is the payload it always was."""
    payload = export.beat_payload(export.authored_beats(CARDED))
    assert [{k: b[k] for k in ("card", "draw_in") if k in b} for b in payload] == [
        {"card": True}, {"draw_in": True}, {}]
    unflagged = export.beat_payload((export.Beat(2, view="map", at="07:00"),))
    flagged_off = export.beat_payload((export.Beat(2, view="map", at="07:00", card=False,
                                                   draw_in=False),))
    assert flagged_off == unflagged and set(unflagged[0]) == {
        "secs", "view", "labels", "at", "speed", "sweep", "hours", "lo", "hi", "tween"}
    for name, beats in export.STORYBOARDS.items():
        for beat in export.beat_payload(beats):
            assert not {"card", "draw_in"} & set(beat), name
    for row in export.storyboard_table():
        for beat in row["beats"]:
            assert not {"card", "draw_in"} & set(beat), row["name"]


def _refusal(beats) -> str:
    """The sentence export.plan's options refuse a list with, as params, which
    is authored_beats' own."""
    with pytest.raises(ValueError) as direct:
        export.authored_beats(beats)
    with pytest.raises(JsonRpcInvalidParams) as caught:
        serve._export_options({"storyboard": beats})
    assert caught.value.data["kind"] == "params"
    assert caught.value.data["hint"] == str(direct.value)
    return str(direct.value)


# Each hint names the beat and the field as one path, `storyboard[1].secs`.
REFUSED = [
    pytest.param([OPENING, {"secs": 0.9, "card": True}], "storyboard[1].secs", "1 second",
                 id="card-0.9s"),
    pytest.param([OPENING, {"secs": 1.9, "draw_in": True}], "storyboard[1].secs", "2 seconds",
                 id="draw-in-1.9s"),
    pytest.param([{**OPENING, "draw_in": True}, {"secs": 2}, {"secs": 2, "draw_in": True}],
                 "storyboard[2].draw_in", "storyboard[0]", id="two-draw-ins"),
    pytest.param([OPENING, {"secs": 2, "view": "linear", "draw_in": True}],
                 "storyboard[1].view", "linear", id="draw-in-on-the-rows"),
    pytest.param([OPENING, {"secs": 2, "view": "time", "draw_in": True}],
                 "storyboard[1].view", "time", id="draw-in-on-the-chart"),
    pytest.param([{**OPENING, "view": "linear"}, {"secs": 2, "draw_in": True}],
                 "storyboard[1].draw_in", "linear", id="draw-in-keeps-the-rows"),
    pytest.param([{**OPENING, "view": "time"}, {"secs": 2, "view": "map", "draw_in": True}],
                 "storyboard[0].view", "undrawn", id="the-chart-before-a-draw-in"),
    pytest.param([OPENING, {"secs": 2, "view": "linear"},
                  {"secs": 2, "view": "map", "draw_in": True}],
                 "storyboard[1].view", "undrawn", id="the-rows-before-a-draw-in"),
    pytest.param([OPENING, {"secs": 2, "draw_in": True, "sweep": True}],
                 "storyboard[1].sweep", "holds", id="a-draw-in-that-sweeps"),
    pytest.param([OPENING, {"secs": 2, "card": "yes"}], "storyboard[1].card", "true or false",
                 id="card-yes"),
    pytest.param([OPENING, {"secs": 2, "draw_in": None}], "storyboard[1].draw_in",
                 "true or false", id="draw-in-null"),
]


@pytest.mark.parametrize("beats,path,also", REFUSED)
def test_a_card_and_a_draw_in_are_refused_by_beat_and_field(beats, path, also):
    hint = _refusal(beats)
    assert path in hint and also in hint, hint


def test_each_new_bound_on_its_edge_is_taken():
    export.authored_beats([{**OPENING, "secs": 1, "view": "geographic", "card": True},
                           {"secs": 2, "draw_in": True}, {"secs": 2, "view": "linear"},
                           {"secs": 1, "view": "time", "card": True}, {"secs": 2, "card": True}])
    # A list may open drawing in, the card over it, and the rows may follow.
    export.authored_beats([{**OPENING, "draw_in": True, "card": True}, {"secs": 1, "view": "time"}])
    # A list with no draw-in may visit any view, as it always could.
    export.authored_beats([{**OPENING, "view": "time", "card": True},
                           {"secs": 1, "view": "linear"}])


def test_a_card_puts_the_city_the_network_and_the_day_on_the_address_title_or_not():
    plain = export.plan(KEY, "instagram-reel", page=APP_PAGE, date=DATE, title=False,
                        storyboard=[OPENING])
    assert not {"city", "network", "date"} & set(_url_query(plain))
    carded = export.plan(KEY, "instagram-reel", page=APP_PAGE, date=DATE, title=False,
                         storyboard=[{**OPENING, "card": True}])
    query = _url_query(carded)
    assert (query["title"], query["city"], query["network"], query["date"]) == (
        "0", "Los Angeles", "Metro Rail", service_day_text(dt.date(2026, 9, 5)))
    # The address is the title's, key for key: the card adds no key of its own.
    titled = export.plan(KEY, "instagram-reel", page=APP_PAGE, date=DATE, storyboard=[OPENING])
    titled_card = export.plan(KEY, "instagram-reel", page=APP_PAGE, date=DATE,
                              storyboard=[{**OPENING, "card": True}])
    assert titled_card.url == titled.url
    assert list(_url_query(carded)) == list(_url_query(titled))
    # url_for itself: a card writes them, and without one nothing moves.
    reel = export.PRESETS["instagram-reel"]
    assert export.url_for(KEY, reel, title=False, date=DATE) == export.url_for(
        KEY, reel, title=False, date=DATE, card=False)
    assert "city=Los+Angeles" in export.url_for(KEY, reel, title=False, date=DATE, card=True)


def test_a_card_too_short_to_read_comes_with_a_note_and_one_long_enough_does_not():
    """The BBC's reading floor, 0.3 s a word: the city, the network and the day
    are eight words, 2.4 s, and an 80-character caption is 14 more."""
    assert export.card_words(KEY, date=DATE) == 8
    assert export.card_words(KEY, date=DATE, caption=CAPTION) == 22

    def notes(secs: float, caption: str | None = None) -> tuple[str, ...]:
        return export.plan(KEY, "instagram-reel", page=APP_PAGE, date=DATE, caption=caption,
                           storyboard=[{**OPENING, "secs": secs, "card": True},
                                       {"secs": 2, "draw_in": True}]).notes

    short = notes(2)
    assert len(short) == 1, short
    assert short[0].startswith("the title card at storyboard[0] says 8 words, about 2.4 seconds")
    assert short[0].endswith("Lengthen the beat.")
    assert notes(2.4) == () and notes(3) == ()
    captioned = notes(6, CAPTION)
    assert len(captioned) == 1 and "22 words, about 6.6 seconds" in captioned[0]
    assert captioned[0].endswith("or shorten the caption.")
    assert notes(6.6, CAPTION) == ()
    # A list without a card, and a still, say nothing of reading.
    assert export.plan(KEY, "instagram-reel", page=APP_PAGE, date=DATE,
                       storyboard=[OPENING]).notes == ()
    assert export.plan(KEY, "instagram-post", page=APP_PAGE, date=DATE,
                       storyboard=[{**OPENING, "secs": 1, "card": True}]).notes == ()


def test_a_plan_with_the_flags_comes_back_through_encode_as_it_went():
    """export.encode reads a plan back field by field: the flags are read as
    the plan writes them, and anything but true or false is refused."""
    job = export.plan(KEY, "instagram-reel", page=APP_PAGE, date=DATE, storyboard=CARDED)
    assert serve._capture_job(job.to_dict()).beats == job.beats
    sent = job.to_dict()
    sent["beats"] = [{**job.beats[0], "card": "yes"}] + list(job.beats[1:])
    with pytest.raises(JsonRpcInvalidParams):
        serve._capture_job(sent)
    # A client that sends a flag as false is read as sending none.
    sent["beats"] = [{**b, "card": False, "draw_in": False} for b in export.beat_payload(
        export.authored_beats([OPENING]))]
    assert serve._capture_job(sent).beats == tuple(export.beat_payload(
        export.authored_beats([OPENING])))


def test_the_schema_takes_the_flags_on_a_beat_and_on_a_plan_and_stays_at_protocol_1():
    check({"storyboard": CARDED}, "ExportOptions")
    assert invalid({"storyboard": [{**OPENING, "card": "yes"}]}, "ExportOptions")
    assert invalid({"storyboard": [{**OPENING, "draw_in": None}]}, "ExportOptions")
    job = export.plan(KEY, "instagram-reel", page=APP_PAGE, date=DATE, storyboard=CARDED)
    check({**job.to_dict(), "filename": job.filename}, "CaptureJob")
    assert serve.PROTOCOL == 1
