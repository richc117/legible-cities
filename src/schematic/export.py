"""Turn a built network into platform-ready assets.

The maps and the animation already exist; this is the part that frames them for
somewhere specific -- a reel, a post, a figure in an essay -- without anyone
guessing at dimensions or re-deriving the same ffmpeg incantation.

Two paths, because the three views are not made in the same place:

* **vector** reads ``site/src/maps/<key>.svg`` and resolves its theme variables
  into literals. Map view only: the linear and stringline views are built in the
  browser and depend on two page CSS rules, so an extracted SVG of them would
  lose its row labels and train halos.
* **raster and video** drive the animation page's presentation mode in a
  headless browser. The page is self-contained and loads over ``file://``, so
  there is no server to start.

Nothing here writes inside the repository. Exports land on the Desktop.

    from schematic import export
    export.run("cdmx-metro", "instagram-reel")
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import feeds

REPO_ROOT = feeds.REPO_ROOT
MAPS_DIR = REPO_ROOT / "site" / "src" / "maps"
RECORDER = REPO_ROOT / "bin" / "_record.js"

# Exports go to the Desktop, never into the repo: they are output, and the repo
# is the method.
DESKTOP = Path.home() / "Desktop" / "legible-cities"


# --------------------------------------------------------------------- palette

# The single source for these colours. They also appear in
# src/schematic/page/page.html and site/src/assets/style.css; test_export.py
# asserts all three agree, because a dark map on a light ground is the kind of
# mistake that only shows up after it is posted.
PALETTES = {
    "dark": {
        "bg": "#15120f",
        "label": "#f2ede6",
        "station-fill": "#f2ede6",
        "station-stroke": "#15120f",
        "train-halo": "#15120f",
    },
    "light": {
        "bg": "#f7efe1",
        "label": "#2d241d",
        "station-fill": "#f7efe1",
        "station-stroke": "#2d241d",
        "train-halo": "#f7efe1",
    },
}

_VAR = re.compile(r"var\(--(?:map-)?([a-z-]+),\s*([^)]*)\)")


def resolve(svg: str, palette: dict[str, str]) -> str:
    """Replace the themed variables with literals, leaving line colours alone.

    A published map carries its furniture colours as CSS custom properties so
    the embedding page can theme it. That works inlined and fails through an
    ``<img>`` or ``rsvg-convert``, which resolve to the fallback -- the light
    value -- and, where a variable has no fallback at all, paint it black.
    """
    return _VAR.sub(lambda m: palette.get(m.group(1), m.group(2).strip()), svg)


# -------------------------------------------------------------------- framing


def padded_box(box: tuple[float, float, float, float], aspect: float,
               frame_top: float = 0.46) -> tuple[float, float, float, float]:
    """Grow a viewBox to ``aspect``. Never crops -- that would cut off stations.

    The extra height is split unevenly on purpose. On a tall frame around a wide
    network the padding is most of the image, and it is the gutter the title and
    clock sit in; centring the network would waste it and push the text onto the
    map.
    """
    x, y, w, h = box
    if w / h < aspect:
        grow = h * aspect - w
        return (x - grow / 2, y, w + grow, h)
    grow = w / aspect - h
    return (x, y - grow * frame_top, w, h + grow)


# -------------------------------------------------------------------- presets


@dataclass(frozen=True)
class Preset:
    """One destination, with the dimensions and limits that destination has."""

    name: str
    platform: str
    width: int
    height: int
    kind: str                      # still | video | vector
    fmt: str                       # png | jpg | mp4 | gif | svg
    view: str = "map"
    labels: bool = True
    storyboard: str = ""           # video only
    fps: int = 30
    max_bytes: int | None = None
    frame_top: float = 0.46
    # Whether the platform draws its own interface over the image. Only true
    # for Instagram's full-bleed portrait surfaces; a Bluesky or LinkedIn image
    # sits in a card with nothing on top of it, so a "safe area" there is a
    # meaningless overlay of somebody else's geometry.
    safe_zones: bool = False
    note: str = ""

    @property
    def aspect(self) -> float:
        return self.width / self.height


PRESETS: dict[str, Preset] = {p.name: p for p in [
    # --- stills --------------------------------------------------------------
    Preset("instagram-post", "Instagram", 1080, 1350, "still", "png"),
    Preset("instagram-square", "Instagram", 1080, 1080, "still", "png"),
    Preset("instagram-story", "Instagram", 1080, 1920, "still", "png",
           safe_zones=True),
    Preset("linkedin", "LinkedIn", 1200, 1200, "still", "png"),
    Preset("linkedin-link", "LinkedIn", 1200, 627, "still", "png",
           note="link-preview shape; the map gets very little height"),
    # Bluesky rejects images over roughly a megabyte, and a dense labelled map
    # exceeds that as PNG. JPEG is not a preference here, it is the only way in.
    Preset("bluesky", "Bluesky", 1200, 900, "still", "jpg", max_bytes=976_000),
    Preset("x", "X", 1600, 900, "still", "png"),

    # --- video ---------------------------------------------------------------
    Preset("instagram-reel", "Instagram", 1080, 1920, "video", "mp4",
           storyboard="tour", safe_zones=True),
    Preset("bluesky-video", "Bluesky", 1080, 1350, "video", "mp4",
           storyboard="tour", max_bytes=50_000_000),
    Preset("linkedin-video", "LinkedIn", 1200, 1200, "video", "mp4",
           storyboard="tour"),

    # --- video, as GIF -------------------------------------------------------
    # The same three shapes again, because a GIF is what plays in a place that
    # will not take a video: an email, a README, a slide. They are deliberately
    # smaller and slower than their mp4 siblings -- a GIF carries a full palette
    # per frame, so resolution and frame rate are what its weight is made of.
    Preset("instagram-reel-gif", "Instagram", 630, 1120, "video", "gif",
           storyboard="morph", fps=12,
           note="9:16 as GIF; half the mp4's size and rate, or it is unusable"),
    Preset("linkedin-gif", "LinkedIn", 640, 640, "video", "gif",
           storyboard="morph", fps=12, note="square GIF"),
    Preset("bluesky-gif", "Bluesky", 640, 800, "video", "gif",
           storyboard="morph", fps=12,
           note="4:5 GIF. Bluesky caps an image at ~1 MB, which nothing this "
                "long will meet -- post the mp4 there and keep this for a page"),

    # --- portfolio and web ---------------------------------------------------
    # The theme pair the essays use: an <img> cannot follow the page's theme, so
    # the page ships both and shows one.
    Preset("portfolio-svg", "Portfolio", 0, 0, "vector", "svg",
           note="both palettes, at the map's own aspect"),
    Preset("portfolio-mp4", "Portfolio", 1200, 900, "video", "mp4",
           storyboard="tour"),
    Preset("portfolio-gif", "Portfolio", 900, 675, "video", "gif",
           storyboard="morph", fps=24,
           note="palette-based GIF; keep it short, they are heavy"),
]}


# ----------------------------------------------------------------- storyboards


@dataclass(frozen=True)
class Beat:
    """A stretch of video with one set of state. Fields left None inherit."""

    secs: float
    view: str | None = None        # geographic | map | linear | time
    labels: bool | None = None
    at: str | None = None          # hard-set the clock, "07:00"
    speed: float | None = None     # simulated seconds per video second
    sweep: bool = False            # run the clock across the beat's whole span
    # What a sweep covers. `hours` runs forward from wherever the clock already
    # is, which is what keeps a storyboard continuous -- a fixed span earlier
    # than the preceding beats made the clock jump backwards on screen. `span`
    # pins absolute times when that is what you want.
    hours: float | None = None
    span: tuple[str, str] | None = None
    tween: float | None = None     # transition length; defaults to min(secs, 1.2)


# Views that need the pre-octilinear geometry, which only feeds with
# Feed.geographic carry. Checked before a browser is launched, because the page
# degrades quietly to the schematic map and an export would look merely dull
# rather than wrong.
GEO_VIEW = "geographic"

# Every view a beat or a preset may name. Single-sourced so adding one cannot
# drift from what the tests and the CLI accept.
VIEWS = (GEO_VIEW, "map", "linear", "time")


# The landing page figure's own timing, from present.js's MORPH and HOLD. Named
# here because `essay-loop` below is that figure and has to stay it.
PAGE_MORPH = 1.8
PAGE_HOLD = 2.6


STORYBOARDS: dict[str, tuple[Beat, ...]] = {
    # The whole argument, in one clip: the network as it sits on the ground,
    # straightened onto the grid, unfolded into a row per line, then re-read as
    # time. Each step discards more geography. Needs Feed.geographic.
    "transform": (
        # tween=0 on the opening beat: every other beat morphs *into* its view,
        # but frame 0 has to already be in one. Without it the clip opens on the
        # schematic map and bends backwards into geography, which is the whole
        # argument told in reverse.
        Beat(4, view=GEO_VIEW, at="08:00", speed=120, tween=0),
        Beat(5, view="map"),
        Beat(5, view="linear"),
        Beat(4, view="time"),
        Beat(9, sweep=True, hours=3),
    ),
    # The same shape without the clock beat, for a short looping figure.
    "transform-loop": (
        Beat(2.5, view=GEO_VIEW, at="08:00", speed=120, tween=0),
        Beat(3, view="map"),
        Beat(3, view="linear"),
        Beat(3, view=GEO_VIEW),
    ),
    # Figure two of the essay, beat for beat: the same cycle `?sequence=transform`
    # runs in the landing page's iframe, so an export of that figure is the
    # figure and not an approximation of it. The page holds 2.6s and morphs over
    # 1.8s, and it returns through the schematic map rather than cutting from
    # the rows back to the ground -- "there and back, so the loop reads as a
    # rewind rather than a jump cut". A beat is hold + tween, hence 4.4.
    # test_export.py reads the two constants out of present.js so the two copies
    # cannot drift.
    "essay-loop": (
        Beat(PAGE_HOLD, view=GEO_VIEW, at="08:00", speed=60, tween=0),
        Beat(PAGE_HOLD + PAGE_MORPH, view="map", tween=PAGE_MORPH),
        Beat(PAGE_HOLD + PAGE_MORPH, view="linear", tween=PAGE_MORPH),
        Beat(PAGE_HOLD + PAGE_MORPH, view="map", tween=PAGE_MORPH),
        Beat(PAGE_HOLD + PAGE_MORPH, view=GEO_VIEW, tween=PAGE_MORPH),
    ),
    # The three views, in the order that explains them.
    "tour": (
        Beat(6, view="map", at="05:30", speed=240),
        Beat(6, view="linear"),
        Beat(3, view="time"),
        # A whole service day stepped over ten seconds moves the clock ~150s per
        # frame, and trains teleport. Sweeping the morning instead keeps the
        # motion readable; see `sweep_rate` below, which warns when it will not.
        Beat(10, sweep=True, hours=4),
    ),
    # Mexico City's shape: the names are the point, and then their absence is.
    "reveal": (
        Beat(4, view="map", labels=True, at="05:30", speed=240),
        Beat(6, labels=False),
        Beat(6, view="linear"),
        Beat(3, view="time"),
        Beat(10, sweep=True, hours=4),
    ),
    # Just the morphs, for a short looping figure.
    "morph": (
        Beat(1.5, view="map", at="08:00", speed=120),
        Beat(2.5, view="linear"),
        Beat(2.5, view="time"),
        Beat(2.5, view="map"),
    ),
    "day": (
        Beat(1, view="map", at="05:00", speed=0),
        Beat(18, sweep=True),
        Beat(1, speed=0),
    ),
    "run": (Beat(20, view="map", at="07:30", speed=240),),
}


# Above this many simulated seconds per frame a sweep reads as flicker.
READABLE_SWEEP = 60.0


def sweep_rate(beat: Beat, fps: int, bounds: tuple[float, float]) -> float:
    """Simulated seconds advanced per frame. Above READABLE_SWEEP it breaks up."""
    if beat.hours:
        covered = beat.hours * 3600
    else:
        lo, hi = _span_seconds(beat, bounds)
        covered = hi - lo
    return covered / max(beat.secs * fps, 1)


def _hms(text: str) -> float:
    parts = [float(v) for v in text.split(":")]
    while len(parts) < 3:
        parts.append(0.0)
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def _span_seconds(beat: Beat, bounds: tuple[float, float]) -> tuple[float, float]:
    if beat.span:
        return (_hms(beat.span[0]), _hms(beat.span[1]))
    return bounds


def frame_count(beats: tuple[Beat, ...], fps: int) -> int:
    return sum(round(b.secs * fps) for b in beats)


# ---------------------------------------------------------------------- naming


def url_for(key: str, preset: Preset, *, view: str | None = None,
            labels: bool | None = None, title: bool = True, clock: bool | None = None,
            theme: str = "dark", at: str | None = None, speed: float | None = None,
            lines: tuple[str, ...] = (), safe: bool = False) -> str:
    """The presentation-mode URL for a preset. Also what you paste into a browser."""
    feed = feeds.FEEDS[key]
    view = view or preset.view
    if clock is None:
        clock = preset.kind == "video"
    q = {
        "present": "1",
        "view": view,
        "labels": "1" if (preset.labels if labels is None else labels) else "0",
        "title": "1" if title else "0",
        "clock": "1" if clock else "0",
        "theme": "dark" if theme == "dark" else "sepia",
    }
    if preset.width and preset.height:
        q["frame"] = f"{preset.width}:{preset.height}"
        q["frametop"] = f"{preset.frame_top}"
    if title:
        q["city"] = feed.city
        q["network"] = feed.network
        # The service day, from the same networks.json the atlas prints. A
        # clock reading 07:14 does not say *when*, and these feeds are
        # snapshots -- an image outlives the page that explains it.
        when = _provenance(key).get("service_date")
        if when:
            q["date"] = dt.date.fromisoformat(when).strftime("%A %-d %B %Y")
    if at:
        q["at"] = at
    if speed is not None:
        q["speed"] = str(speed)
    if lines:
        q["lines"] = ",".join(lines)
    if safe:
        q["safe"] = "1"
    from urllib.parse import urlencode
    return (MAPS_DIR / f"{key}.html").as_uri() + "?" + urlencode(q)


def wants_geographic(*, view: str | None = None, preset: Preset | None = None,
                     storyboard: str | None = None) -> bool:
    """Whether this export asks for the pre-octilinear geometry anywhere."""
    if view == GEO_VIEW:
        return True
    if view is None and preset is not None and preset.view == GEO_VIEW:
        return True
    name = storyboard or (preset.storyboard if preset else "")
    return any(b.view == GEO_VIEW for b in STORYBOARDS.get(name, ()))


def check_geographic(key: str, *, view: str | None = None,
                     preset: Preset | None = None,
                     storyboard: str | None = None) -> None:
    """Refuse a geographic export of a feed that has no geographic geometry.

    The page degrades quietly here -- with nothing to raise, it simply shows the
    schematic map -- so the export would come out looking dull rather than
    broken, and only on review. Better to say which switch is off.
    """
    if not wants_geographic(view=view, preset=preset, storyboard=storyboard):
        return
    if feeds.FEEDS[key].geographic:
        return
    raise ValueError(
        f"{key!r} carries no geographic geometry, so the geographic view would "
        f"silently render as the schematic map. Set geographic=True on its "
        f"Feed in feeds.py and rebuild it (bin/build-site, or "
        f"schematic.site.export()); it is off by default because it is a "
        f"second copy of every track. Feeds that have it: "
        + ", ".join(sorted(k for k, f in feeds.FEEDS.items() if f.geographic)))


VIEW_PHRASE = {
    GEO_VIEW: "where its track actually runs",
    "map": "straightened onto a 45-degree grid",
    "linear": "with every line pulled out into its own row of evenly spaced stations",
    "time": "as a time chart, every train a diagonal",
}


def storyboard_views(name: str) -> str:
    """The views a storyboard visits, in order, as one readable field."""
    seen: list[str] = []
    for b in STORYBOARDS.get(name, ()):
        if b.view and b.view not in seen:
            seen.append(b.view)
    return " -> ".join(seen)


def storyboard_alt(key: str, name: str, *, stations: int = 0, lines: int = 0) -> str:
    """Alt text for a clip that passes through several views.

    A storyboard video described as its preset's single view is simply wrong --
    "schematic map of..." for a clip that opens on geography and ends on a
    chart. The views it visits are the description.
    """
    beats = STORYBOARDS.get(name, ())
    seen: list[str] = []
    for b in beats:
        if b.view and b.view not in seen:
            seen.append(b.view)
    if len(seen) < 2:
        return alt_text(key, seen[0] if seen else "map", stations=stations, lines=lines)

    feed = feeds.FEEDS[key]
    where = f"the {feed.city} {feed.network}".replace("the the ", "the ")
    steps = [VIEW_PHRASE.get(v, v) for v in seen]
    joined = ", then ".join(steps)
    counts = []
    if lines:
        counts.append(f"{lines} lines in the operator's own colours")
    if stations:
        counts.append(f"{stations} stations")
    tail = (" " + ", ".join(counts) + ".") if counts else ""
    return (f"An animation of {where}, running a real day's timetable: drawn "
            f"{joined}.{tail}").strip()


def alt_text(key: str, view: str, *, stations: int = 0, lines: int = 0) -> str:
    """A description worth pasting. The project argues for legibility; an export
    that ships without one undercuts its own point."""
    feed = feeds.FEEDS[key]
    where = f"the {feed.city} {feed.network}".replace("the the ", "the ")
    counts = []
    if lines:
        counts.append(f"{lines} lines in the operator's own colours")
    if stations:
        counts.append(f"{stations} stations")
    tail = (", ".join(counts) + ". ") if counts else ""
    if view == "linear":
        return (f"Every line of {where} drawn as a row of evenly spaced stations, "
                f"geography removed. {tail}").strip()
    if view == "time":
        return (f"A chart of a whole service day on {where}: time runs left to "
                f"right, stations down each line's band, and every diagonal is "
                f"one train. {tail}").strip()
    if view == GEO_VIEW:
        return (f"{where[:1].upper()}{where[1:]} drawn where its track actually runs, "
                f"before the schematic straightens it. {tail}").strip()
    return (f"Schematic map of {where}, with every segment running horizontally, "
            f"vertically or at forty-five degrees. {tail}").strip()


# ---------------------------------------------------------------------- output


def line_labels(key: str) -> list[str]:
    """Every line label on this network, from the atlas's own data."""
    return _provenance(key).get("labels") or _labels_from_svg(key)


def _labels_from_svg(key: str) -> list[str]:
    svg = MAPS_DIR / f"{key}.svg"
    if not svg.exists():
        return []
    return sorted(set(re.findall(r'data-line="([^"]+)"', svg.read_text())))


def keep_except(key: str, drop: tuple[str, ...]) -> tuple[str, ...]:
    """Everything but these. A single outlying line can dominate the frame --
    New York's Staten Island Railway is genuinely disconnected from the rest of
    the system and sits off on its own diagonal, roughly doubling the bounding
    box and shrinking the subway to fit beside it."""
    labels = line_labels(key)
    unknown = [d for d in drop if d not in labels]
    if unknown:
        raise ValueError(f"{key} has no line(s) {unknown}. It has: {', '.join(labels)}")
    return tuple(l for l in labels if l not in drop)


def desktop_dir(key: str, out: Path | None = None) -> Path:
    dest = (out or DESKTOP) / key
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _guard_outside_repo(dest: Path) -> None:
    if REPO_ROOT in dest.resolve().parents or dest.resolve() == REPO_ROOT:
        raise ValueError(f"refusing to export into the repository: {dest}")


def check_size(path: Path, preset: Preset) -> None:
    """Fail loudly rather than let a file be silently rejected at upload."""
    if preset.max_bytes and path.stat().st_size > preset.max_bytes:
        raise ValueError(
            f"{path.name} is {path.stat().st_size/1e6:.1f} MB, over "
            f"{preset.platform}'s {preset.max_bytes/1e6:.1f} MB limit")


def _run_recorder(job: dict) -> None:
    proc = subprocess.run(["node", str(RECORDER), json.dumps(job)],
                          capture_output=True, text=True)
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            print("  " + line)
    if proc.returncode:
        raise RuntimeError("capture failed")


def beat_payload(beats: tuple[Beat, ...],
                 bounds: tuple[float, float] = (0.0, 86_400.0)) -> list[dict]:
    """Beats as ``bin/_record.js`` wants them.

    Its own function so a caller other than ``run`` -- the determinism test --
    drives the recorder through exactly the shape a real export does, rather
    than through a hand-built copy that can drift from it. ``bounds`` is the
    clock's range, so a sweep with no explicit span covers whatever service day
    the network actually has.
    """
    out = []
    for b in beats:
        lo, hi = _span_seconds(b, bounds)
        out.append({
            "secs": b.secs, "view": b.view, "labels": b.labels,
            "at": _hms(b.at) if b.at else None, "speed": b.speed,
            "sweep": b.sweep, "hours": b.hours,
            "lo": None if b.hours else lo,
            "hi": None if b.hours else hi,
            "tween": b.tween if b.tween is not None else min(b.secs, 1.2),
        })
    return out


def _resample(src: Path, dest: Path, preset: Preset) -> None:
    """Down to the preset's exact size. Lanczos, because these maps are mostly
    one-pixel strokes and a box filter turns them to mush."""
    q = ["-q:v", "3"] if preset.fmt == "jpg" else []
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
                    "-vf", f"scale={preset.width}:{preset.height}:flags=lanczos",
                    *q, str(dest)], check=True)


def _encode(frames: Path, dest: Path, preset: Preset, *, fade: float = 0.0,
            crf: int = 20, keep: bool = False) -> None:
    """PNG sequence to a deliverable. Text never enters here.

    This ffmpeg has no drawtext, no subtitles and no freetype, so it cannot
    render a glyph. Every word in an export is drawn by the page.
    """
    src = str(frames / "%06d.png")
    if preset.fmt == "gif":
        # Two passes: a palette built from the actual frames, then applied.
        # A single pass would quantise to the default 216-colour cube and the
        # agency line colours would shift.
        palette = frames / "palette.png"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(preset.fps),
                        "-i", src, "-vf", "palettegen=stats_mode=diff", str(palette)],
                       check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(preset.fps),
                        "-i", src, "-i", str(palette), "-lavfi",
                        f"scale={preset.width}:{preset.height}:flags=lanczos[s];"
                        "[s][1:v]paletteuse=dither=bayer:bayer_scale=3", str(dest)],
                       check=True)
        return

    n = len(list(frames.glob("*.png")))
    dur = n / preset.fps
    vf = ["format=yuv420p"]
    if not keep:
        vf.insert(0, f"scale={preset.width}:{preset.height}:flags=lanczos")
    if fade > 0:
        vf = [f"fade=t=in:st=0:d={fade}",
              f"fade=t=out:st={max(dur - fade, 0):.2f}:d={fade}"] + vf
    subprocess.run([
        "ffmpeg", "-y", "-loglevel", "error",
        "-framerate", str(preset.fps), "-i", src,
        # Several platforms mishandle a video with no audio stream at all, and
        # give no useful error when they do.
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", str(crf),
        "-r", str(preset.fps), "-c:a", "aac", "-b:a", "96k", "-shortest",
        "-movflags", "+faststart", str(dest)], check=True)


def _vector(key: str, dest: Path, preset: Preset) -> list[Path]:
    """Theme-paired SVG, straight from the built map. No browser involved."""
    source = MAPS_DIR / f"{key}.svg"
    if not source.exists():
        raise FileNotFoundError(f"{source} is missing; run bin/build-site first")
    svg = source.read_text()
    out = []
    for theme, palette in PALETTES.items():
        path = dest / f"{key}-{theme}.svg"
        path.write_text(resolve(svg, palette))
        out.append(path)
    return out


def run(key: str, preset_name: str, *, theme: str = "dark", view: str | None = None,
        labels: bool | None = None, title: bool = True, clock: bool | None = None,
        at: str | None = None, lines: tuple[str, ...] = (),
        storyboard: str | None = None, quality: str = "standard", fade: float = 0.0,
        safe: bool = False, tag: str = "", out: Path | None = None) -> list[Path]:
    """Export one network for one destination. Returns what it wrote.

    ``clock`` and ``title`` are the two pieces of furniture the page draws over
    the map; both off is the essay's own figure, which carries neither. ``tag``
    goes into the filename, so two dressings of the same preset -- with the name
    and without it -- can sit in one folder instead of overwriting each other.
    """
    if key not in feeds.FEEDS:
        raise KeyError(f"unknown feed {key!r}")
    preset = PRESETS[preset_name]
    check_geographic(key, view=view, preset=preset, storyboard=storyboard)
    dest = desktop_dir(key, out)
    _guard_outside_repo(dest)

    if preset.kind == "vector":
        written = _vector(key, dest, preset)
    else:
        # `scale` supersamples the capture; `keep` decides whether the extra
        # pixels are delivered or spent on resampling. A platform that expects
        # 1080 wide does better with a clean 1080 than with a 2160 it downscales
        # itself -- and these maps are full of 1px strokes, which is exactly what
        # a bad downscale ruins. "high" keeps them, for print and retina.
        scale, keep, crf = {
            "draft": (1, True, 26),
            "standard": (2, False, 20),
            "high": (2, True, 16),
        }[quality]
        stem = (f"{key}-{preset.name}" + (f"-{theme}" if theme != "dark" else "")
                + (f"-{tag}" if tag else ""))
        url = url_for(key, preset, view=view, labels=labels, title=title,
                      clock=clock, theme=theme, at=at, lines=lines, safe=safe)
        job = {"url": url, "width": preset.width, "height": preset.height,
               "scale": scale, "fps": preset.fps, "format": preset.fmt}

        if preset.kind == "still":
            path = dest / f"{stem}.{preset.fmt}"
            if keep:
                _run_recorder({**job, "mode": "still", "out": str(path)})
            else:
                with tempfile.TemporaryDirectory(prefix="legible-still-") as tmp:
                    big = Path(tmp) / f"big.{preset.fmt}"
                    _run_recorder({**job, "mode": "still", "out": str(big)})
                    _resample(big, path, preset)
            written = [path]
        else:
            beats = STORYBOARDS[storyboard or preset.storyboard]
            # A sweep faster than this stops reading as motion: a train that
            # lives 2,000 seconds appears in a handful of frames and jumps
            # between them. Worth saying before spending a minute capturing it.
            for b in beats:
                if b.sweep:
                    rate = sweep_rate(b, preset.fps, (0.0, 86_400.0))
                    if rate > READABLE_SWEEP:
                        print(f"  note: this sweep advances {rate:.0f} simulated "
                              f"seconds per frame, so trains will jump rather than "
                              f"move. Narrow the beat's span, or lengthen it.")
            with tempfile.TemporaryDirectory(prefix="legible-frames-") as tmp:
                frames = Path(tmp)
                # The clock bounds are the feed's, so a sweep with no explicit
                # span covers whatever service day this network actually has.
                _run_recorder({**job, "mode": "video", "frames": str(frames),
                               "beats": beat_payload(beats)})
                path = dest / f"{stem}.{preset.fmt}"
                _encode(frames, path, preset, fade=fade, crf=crf, keep=keep)
            written = [path]

    for path in written:
        check_size(path, preset)
    _write_sidecar(key, preset, written, theme=theme, view=view or preset.view,
                   storyboard=(storyboard or preset.storyboard)
                   if preset.kind == "video" else "")
    return written


def _write_sidecar(key: str, preset: Preset, written: list[Path], *,
                   theme: str, view: str, storyboard: str = "") -> None:
    """What this file is, beside the file. Includes the caveats the atlas shows:
    an image travels further than the page it came from."""
    feed = feeds.FEEDS[key]
    prov = _provenance(key)
    stats = {k: prov[k] for k in ("stations", "lines") if k in prov} or _network_stats(key)
    for path in written:
        meta = {
            "file": path.name,
            "bytes": path.stat().st_size,
            "feed": key,
            "city": feed.city,
            "network": feed.network,
            "preset": preset.name,
            "platform": preset.platform,
            "size": f"{preset.width}x{preset.height}" if preset.width else "native",
            # A storyboard visits several views, so naming one of them here
            # would misdescribe the file it sits beside.
            "view": storyboard_views(storyboard) or view,
            "storyboard": storyboard or None,
            "theme": theme,
            "alt": (storyboard_alt(key, storyboard, **stats) if storyboard
                    else alt_text(key, view, **stats)),
            "service_date": prov.get("service_date"),
            "trips": prov.get("trips"),
            # What the atlas says about this network, carried with the picture.
            "caveats": prov.get("caveats", []),
            "notes": list(feed.notes),
            "source": feed.url,
        }
        path.with_suffix(path.suffix + ".json").write_text(json.dumps(meta, indent=2) + "\n")


def _provenance(key: str) -> dict:
    """The atlas's own numbers and caveats for this network.

    Read from the generated networks.json rather than recomputed: it is already
    the single source the site uses, and an image travels further than the page
    it came from, so whatever the atlas admits should travel with it.
    """
    path = REPO_ROOT / "site" / "src" / "_data" / "networks.json"
    if not path.exists():
        return {}
    for entry in json.loads(path.read_text()).get("networks", []):
        if entry["key"] == key:
            return {"service_date": entry["date"],
                    "stations": entry["stations"],
                    "lines": len(entry["lines"]),
                    "labels": list(entry["lines"]),
                    "trips": entry["trips"],
                    "caveats": entry["caveats"]}
    return {}


def _network_stats(key: str) -> dict:
    """Station and line counts, read from the built map rather than recomputed."""
    svg = MAPS_DIR / f"{key}.svg"
    if not svg.exists():
        return {}
    text = svg.read_text()
    return {"stations": text.count("<circle"),
            "lines": len(set(re.findall(r'data-line="([^"]+)"', text)))}


def poster(video: Path, dest: Path, at: float = 0.6) -> Path:
    """A representative frame, for a contact sheet or a video cover."""
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss",
                    f"{at * _duration(video):.2f}", "-i", str(video),
                    "-frames:v", "1", "-q:v", "2", str(dest)], check=True)
    return dest


def _duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def contact_sheet(paths: list[Path], dest: Path, columns: int = 3) -> Path:
    """One picture of everything a run produced.

    Composed as SVG and rasterised rather than tiled with ffmpeg, because the
    captions need text and this ffmpeg cannot draw a glyph. librsvg resolves
    relative references against the input file's directory, so the SVG has to be
    written beside the images rather than piped in.
    """
    cell_w, cell_h, pad, caption = 420, 420, 18, 34
    shown: list[tuple[str, str]] = []
    with tempfile.TemporaryDirectory(prefix="legible-sheet-") as tmp:
        work = Path(tmp)
        # librsvg refuses to load a file:// reference outside the SVG's own
        # directory, so every tile is copied in beside the sheet and referenced
        # by bare filename. An SVG tile is rasterised first: nesting one inside
        # an <image> is not reliably supported.
        for i, src in enumerate(paths):
            if src.suffix in {".mp4", ".gif"}:
                poster(src, work / f"{i:02d}.jpg")
                shown.append((f"{i:02d}.jpg", src.name))
            elif src.suffix == ".svg":
                subprocess.run(["rsvg-convert", "-w", str(cell_w * 2), "-f", "png",
                                "-o", str(work / f"{i:02d}.png"), str(src)], check=True)
                shown.append((f"{i:02d}.png", src.name))
            elif src.suffix in {".png", ".jpg"}:
                shutil.copyfile(src, work / f"{i:02d}{src.suffix}")
                shown.append((f"{i:02d}{src.suffix}", src.name))
        if not shown:
            raise ValueError("nothing to put on a contact sheet")

        rows = (len(shown) + columns - 1) // columns
        w = columns * cell_w + (columns + 1) * pad
        h = rows * (cell_h + caption) + (rows + 1) * pad
        bg, fg = PALETTES["dark"]["bg"], PALETTES["dark"]["label"]
        out = [f'<svg xmlns="http://www.w3.org/2000/svg" '
               f'xmlns:xlink="http://www.w3.org/1999/xlink" width="{w}" height="{h}" '
               f'viewBox="0 0 {w} {h}" font-family="Helvetica Neue, Helvetica, Arial, sans-serif">',
               f'<rect width="{w}" height="{h}" fill="{bg}"/>']
        for i, (img, label) in enumerate(shown):
            cx = pad + (i % columns) * (cell_w + pad)
            cy = pad + (i // columns) * (cell_h + caption + pad)
            out.append(f'<image x="{cx}" y="{cy}" width="{cell_w}" height="{cell_h}" '
                       f'preserveAspectRatio="xMidYMid meet" xlink:href="{img}"/>')
            out.append(f'<text x="{cx}" y="{cy + cell_h + 22}" font-size="15" '
                       f'fill="{fg}" opacity="0.75">{label}</text>')
        out.append("</svg>")
        sheet = work / "sheet.svg"
        sheet.write_text("\n".join(out))
        subprocess.run(["rsvg-convert", "-w", str(w), "-f", "png",
                        "-o", str(dest), str(sheet)], check=True)
    return dest


def safe_preview(key: str, preset: Preset, *, out: Path | None = None,
                 theme: str = "dark", view: str | None = None) -> list[Path]:
    """Where the platform's own UI will cover the frame.

    Written as its own ``-safe`` file and never as a deliverable: a preview that
    could be posted by accident is worse than no preview.
    """
    if preset.kind == "vector":
        return []
    if not preset.safe_zones:
        raise ValueError(
            f"{preset.platform} does not draw its interface over the image, so a "
            f"safe-area preview for {preset.name} would just be Instagram's "
            f"geometry on someone else's canvas. Try instagram-reel or "
            f"instagram-story.")
    # Its own folder, and never beside a deliverable: the whole failure mode is
    # picking up the one with the guides on it.
    dest = desktop_dir(key, out) / "safe-area"
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{key}-{preset.name}-safe.png"
    url = url_for(key, preset, view=view, theme=theme, safe=True)
    _run_recorder({"url": url, "width": preset.width, "height": preset.height,
                   "scale": 1, "fps": preset.fps, "format": "png",
                   "mode": "still", "out": str(path)})
    return [path]
