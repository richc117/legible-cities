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

An export is three halves, so another process can do the middle: ``plan``
describes a capture and is pure; ``capture`` runs the recorder; ``encode``
turns what was captured into the deliverable and writes its sidecar. ``run``
composes them for the CLI. The desktop app captures for itself (its ADR-024)
and asks this module only for the plan and the encode.

Nothing here writes inside the repository. Exports land on the Desktop.

    from schematic import export
    export.run("cdmx-metro", "instagram-reel")
"""

from __future__ import annotations

import datetime as dt
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Sequence

from . import feeds, loom
from .config import REPO_ROOT
from .schedule import service_day_text

# What an encode reports as it goes: a stage, a fraction and a sentence, the
# shape the server's job/progress notification carries.
Progress = Callable[[str, float, str], None]

# Anchored to the repository, not to the engine's home: the recorder is code,
# and the site's map folder is the site's, whatever SCHEMATIC_HOME says.
MAPS_DIR = REPO_ROOT / "site" / "src" / "maps"
RECORDER = REPO_ROOT / "bin" / "_record.js"

# Exports go to the Desktop, never into the repo: they are output, and the repo
# is the method.
DESKTOP = Path.home() / "Desktop" / "legible-cities"

# Milliseconds the recorder allows, with the clock already stopped, for the
# page's first geometry pass and its fonts. The payload is megabytes on the
# larger networks.
SETTLE_MS = 1200

# Where the page's clock starts when the URL does not say (`let now = 7 * 3600`
# in page.html; test_export.py holds the two together). A still is taken at
# this clock unless `at` says otherwise, and the recorder seeks to it after
# stopping the clock, because the page's own loop has already run for the
# few frames between load and the recorder's first word.
PAGE_START = "07:00"


def ffmpeg_path() -> str:
    """The ffmpeg every encode runs: ``SCHEMATIC_FFMPEG`` when set, else the
    one on PATH. The one place the name lives, so a bundled binary reaches
    every call and ``engine.info`` reports the same thing the encoder uses."""
    return os.environ.get("SCHEMATIC_FFMPEG") or "ffmpeg"


def ffprobe_path() -> str:
    """ffprobe beside the ffmpeg ``SCHEMATIC_FFMPEG`` names, else the one on PATH."""
    given = os.environ.get("SCHEMATIC_FFMPEG")
    if given and "ffmpeg" in Path(given).name:
        return str(Path(given).with_name(Path(given).name.replace("ffmpeg", "ffprobe", 1)))
    return "ffprobe"


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
    # The row of SAFE_ZONES for the interface the platform draws over the
    # image, or "" for none. Only Instagram's full-bleed portrait surfaces have
    # one; a Bluesky or LinkedIn image sits in a card with nothing on top of
    # it, so a "safe area" there is a meaningless overlay of somebody else's
    # geometry.
    zones: str = ""
    note: str = ""

    @property
    def aspect(self) -> float:
        return self.width / self.height

    @property
    def safe_zones(self) -> bool:
        """Whether the platform draws its own interface over the image."""
        return bool(self.zones)


PRESETS: dict[str, Preset] = {p.name: p for p in [
    # --- stills --------------------------------------------------------------
    Preset("instagram-post", "Instagram", 1080, 1350, "still", "png"),
    Preset("instagram-square", "Instagram", 1080, 1080, "still", "png"),
    Preset("instagram-story", "Instagram", 1080, 1920, "still", "png",
           zones="instagram-stories"),
    Preset("linkedin", "LinkedIn", 1200, 1200, "still", "png"),
    Preset("linkedin-link", "LinkedIn", 1200, 627, "still", "png",
           note="link-preview shape; the map gets very little height"),
    # Bluesky rejects images over roughly a megabyte, and a dense labelled map
    # exceeds that as PNG. JPEG is not a preference here, it is the only way in.
    Preset("bluesky", "Bluesky", 1200, 900, "still", "jpg", max_bytes=976_000),
    Preset("x", "X", 1600, 900, "still", "png"),

    # --- video ---------------------------------------------------------------
    Preset("instagram-reel", "Instagram", 1080, 1920, "video", "mp4",
           storyboard="tour", zones="instagram-reels"),
    # Bluesky's own client allows 300 MB a video (read 6 Oct 2026). The 50 MB
    # this said before refused a long export at high quality, and only after
    # its capture had run.
    Preset("bluesky-video", "Bluesky", 1080, 1350, "video", "mp4",
           storyboard="tour", max_bytes=300_000_000),
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


@dataclass(frozen=True)
class Zones:
    """Where a platform draws its own interface over a full-bleed frame.

    Fractions of the frame: ``top``, ``bottom`` and ``rail_top`` of its height,
    ``side`` and ``rail_width`` of its width; None where the platform has no
    such zone. The rail is the column of buttons in the lower right, from
    ``rail_top`` down to the bottom edge. ``measured`` is the day the numbers
    were read, because platforms move their interfaces: a row is a dated
    reading, not a fact.
    """

    top: float | None
    bottom: float | None
    side: float | None
    rail_width: float | None
    rail_top: float | None
    measured: dt.date
    source: str


# The platforms' interfaces, by the name a preset's ``zones`` gives. The plan
# writes a row's fractions onto the page's address and the page draws and
# avoids what it is given, so these are the only zone numbers there are
# (app ADR-052). A platform without a preset has no row.
SAFE_ZONES: dict[str, Zones] = {
    "instagram-reels": Zones(
        top=0.14, bottom=0.35, side=0.06, rail_width=0.21,
        # Not read from Meta's file: a summariser's reading of the rail's top
        # at about 1,150 px down, and unverified (ADR-052's Decision says so).
        rail_top=0.60,
        measured=dt.date(2026, 10, 2),
        source="Meta's Reels template as traced by Cadenus (July 2026): 270 px "
               "clear at the top, 672 at the bottom and 64 at each side, and the "
               "button rail 227 px from the right edge, at 1080x1920."),
    "instagram-stories": Zones(
        top=0.14, bottom=0.20, side=None, rail_width=None, rail_top=None,
        measured=dt.date(2026, 10, 2),
        source="Meta's Stories template as traced by Cadenus (July 2026): "
               "\"roughly the top 14 percent (about 270 pixels) and the bottom "
               "20 percent (about 385 pixels)\"."),
}

# Where the page can put the clock. Bottom right is where it has always sat,
# and it is the default everywhere but on a preset with a row in SAFE_ZONES.
CLOCK_CORNERS = ("top-left", "top-right", "bottom-left", "bottom-right")
DEFAULT_CORNER = "bottom-right"

# A caption is a person's text, drawn by the page as text under the title.
# Eighty is two lines at the overlay's size on every preset (ADR-052), and is
# this project's bound, not a platform's.
CAPTION_MAX = 80
LINE_BREAKS = ("\r", "\n", "\u2028", "\u2029")


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

# What a plan names for a storyboard a client wrote as a list of beats rather
# than chose by name. Never a key of STORYBOARDS.
CUSTOM = "custom"


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


# A time of day as the protocol writes one, its `Clock`. The server compiles
# this same text, so what a beat's clock may be cannot drift from what an
# export's `at` may be.
CLOCK = r"^[0-9]{1,2}:[0-9]{2}(:[0-9]{2})?$"
_CLOCK = re.compile(CLOCK)

# The bounds of a storyboard a client writes (the desktop app's ADR-051). The
# 90 seconds is that record's number, not a platform's: 2,700 frames at 30 fps,
# about seven minutes of capture.
MAX_BEATS = 16
BEAT_SECS = (0.5, 30.0)
MAX_SECONDS = 90.0
MAX_HOURS = 24.0

# A beat's fields as the protocol's StoryboardBeat names them.
BEAT_FIELDS = ("secs", "view", "labels", "at", "speed", "sweep", "hours", "span", "tween")

# A list's first beat says where it opens, so nothing beside the list may.
BESIDE_A_LIST = "view and at go on a list's first beat (storyboard[0]), not beside the list"


def _number(value: object) -> bool:
    """A number as JSON carries one: not a bool, and finite."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value))


def _clock(value: object) -> bool:
    return isinstance(value, str) and bool(_CLOCK.fullmatch(value))


def authored_beats(beats: Sequence[Beat | dict]) -> tuple[Beat, ...]:
    """A storyboard a client wrote, checked, as ``Beat``s.

    ``beats`` holds objects with ``StoryboardBeat``'s fields, as the protocol
    carries them, or ``Beat``s. A refusal is a ``ValueError`` with one
    sentence naming the beat, counted from 0 (``storyboard[2]``), and the
    field. Frame 0 has to already be in a view, so the first beat names one
    and does not transition into it: a ``tween`` left out or null there is
    read as 0, and any other is refused. Frame 0 also has to be at a known
    time, or two captures of one plan differ, so the first beat names ``at``
    unless it sweeps without ``hours``, whose span the recorder seeks to
    itself: the desktop app's capture refuses any other first beat, and this
    is its rule. Later beats are kept as written, so a null ``tween`` stays
    null and ``beat_payload`` gives it its default.
    """
    if not isinstance(beats, (list, tuple)):
        raise ValueError("storyboard must be a storyboard's name or a list of beats")
    if not beats:
        raise ValueError(f"storyboard holds no beats: a list holds 1 to {MAX_BEATS}")
    if len(beats) > MAX_BEATS:
        raise ValueError(f"storyboard[{MAX_BEATS}] is one beat too many: a list holds "
                         f"1 to {MAX_BEATS} beats")
    low, high = BEAT_SECS
    out: list[Beat] = []
    total = 0.0
    for i, raw in enumerate(beats):
        where = f"storyboard[{i}]"
        if isinstance(raw, Beat):
            fields = {name: getattr(raw, name) for name in BEAT_FIELDS}
        elif isinstance(raw, dict):
            fields = dict(raw)
        else:
            raise ValueError(f"{where} must be an object with a beat's fields")
        extra = [str(name) for name in fields if name not in BEAT_FIELDS]
        if extra:
            raise ValueError(f"{where} does not take {', '.join(extra)}; a beat's fields are "
                             + ", ".join(BEAT_FIELDS))
        secs = fields.get("secs")
        if not (_number(secs) and low <= secs <= high):
            raise ValueError(f"{where}.secs must be seconds, from {low:g} to {high:g}")
        total = round(total + secs, 6)
        if total > MAX_SECONDS:
            raise ValueError(f"{where}.secs brings the storyboard to {total:g} seconds, past "
                             f"the {MAX_SECONDS:g} seconds a list may last")
        view = fields.get("view")
        if view is not None and view not in VIEWS:
            raise ValueError(f"{where}.view must be one of {', '.join(VIEWS)}, or null")
        if i == 0 and view is None:
            raise ValueError(f"{where}.view is missing: the first beat names the view "
                             f"frame 0 is in")
        labels = fields.get("labels")
        if labels is not None and not isinstance(labels, bool):
            raise ValueError(f"{where}.labels must be true, false or null")
        at = fields.get("at")
        if at is not None and not _clock(at):
            raise ValueError(f"{where}.at must be a clock, HH:MM, or null")
        speed = fields.get("speed")
        if speed is not None and not (_number(speed) and speed >= 0):
            raise ValueError(f"{where}.speed must be simulated seconds a second, 0 or "
                             f"more, or null")
        sweep = fields.get("sweep", False)
        if not isinstance(sweep, bool):
            raise ValueError(f"{where}.sweep must be true or false")
        hours = fields.get("hours")
        if hours is not None and not (_number(hours) and 0 < hours <= MAX_HOURS):
            raise ValueError(f"{where}.hours must be more than 0 and at most {MAX_HOURS:g}, "
                             f"or null")
        span = fields.get("span")
        if span is not None:
            if not (isinstance(span, (list, tuple)) and len(span) == 2
                    and all(_clock(c) for c in span)):
                raise ValueError(f"{where}.span must be two clocks, HH:MM, or null")
            if not _hms(span[0]) < _hms(span[1]):
                raise ValueError(f"{where}.span must run forward: {span[0]} is not "
                                 f"before {span[1]}")
            span = (span[0], span[1])
        tween = fields.get("tween")
        if tween is not None and not (_number(tween) and tween >= 0):
            raise ValueError(f"{where}.tween must be seconds, 0 or more, or null")
        if i == 0:
            if tween is None:
                tween = 0
            elif tween != 0:
                raise ValueError(f"{where}.tween must be 0 or left out: frame 0 must "
                                 f"already be in a view, so the first beat cannot "
                                 f"transition into one")
            # A sweep with no hours carries its span's two ends to the
            # recorder, which seeks to the first; anything else starts
            # wherever the page's clock happened to be.
            if at is None and not (sweep and hours is None):
                raise ValueError(f"{where}.at is missing: frame 0 is not reproducible "
                                 f"without a clock, so the first beat names one, unless "
                                 f"it sweeps a span rather than a number of hours")
        out.append(Beat(secs, view=view, labels=labels, at=at, speed=speed, sweep=sweep,
                        hours=hours, span=span, tween=tween))
    return tuple(out)


# ---------------------------------------------------------------------- naming


def url_for(key: str, preset: Preset, *, view: str | None = None,
            labels: bool | None = None, title: bool = True, clock: bool | None = None,
            theme: str = "dark", at: str | None = None, speed: float | None = None,
            lines: tuple[str, ...] = (), safe: bool = False,
            page: str | None = None, date: str | None = None,
            caption: str | None = None, clock_corner: str = DEFAULT_CORNER,
            zones: Zones | None = None) -> str:
    """The presentation-mode URL for a preset. Also what you paste into a browser.

    ``page`` is the page's own address when it is not the site's file: the
    desktop app serves a project's page on its own origin and passes it here.
    ``date`` (YYYY-MM-DD) is the service day the title names when the caller
    knows it; otherwise it is the atlas's.

    ``caption``, ``clock_corner`` and ``zones`` are written after everything
    else, and only where they say something, so an address that uses none of
    them is the one it always was. The page lays its frame out below a top
    zone and keeps the name and the clock off the side zones; it draws the
    zones themselves only under ``safe``.
    """
    if clock_corner not in CLOCK_CORNERS:
        raise ValueError(f"clock_corner is one of {', '.join(CLOCK_CORNERS)}, "
                         f"not {clock_corner!r}")
    feed = feeds.get(key)
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
        when = date or _provenance(key).get("service_date")
        if when:
            q["date"] = service_day_text(dt.date.fromisoformat(when))
    if at:
        q["at"] = at
    if speed is not None:
        q["speed"] = str(speed)
    if lines:
        q["lines"] = ",".join(lines)
    if safe:
        q["safe"] = "1"
    if caption:
        q["caption"] = caption
    if clock and clock_corner != DEFAULT_CORNER:
        q["corner"] = clock_corner
    if zones is not None:
        for name, value in (("ztop", zones.top), ("zbottom", zones.bottom),
                            ("zside", zones.side), ("zrail", zones.rail_width),
                            ("zrailtop", zones.rail_top)):
            if value is not None:
                q[name] = str(value)
    from urllib.parse import urlencode
    base = page or (MAPS_DIR / f"{key}.html").as_uri()
    return base + "?" + urlencode(q)


def _beats_of(storyboard: str | Sequence[Beat | dict] | None) -> tuple[Beat | dict, ...]:
    """A storyboard's beats, given its name or the beats themselves: ``Beat``s,
    or the payloads a plan carries, whose ``view`` is the same."""
    if isinstance(storyboard, str):
        return STORYBOARDS.get(storyboard, ())
    return tuple(storyboard or ())


def _view_of(beat: Beat | dict) -> str | None:
    return beat.get("view") if isinstance(beat, dict) else beat.view


def _views_visited(storyboard: str | Sequence[Beat | dict] | None) -> list[str]:
    seen: list[str] = []
    for b in _beats_of(storyboard):
        view = _view_of(b)
        if view and view not in seen:
            seen.append(view)
    return seen


def wants_geographic(*, view: str | None = None, preset: Preset | None = None,
                     storyboard: str | Sequence[Beat | dict] | None = None) -> bool:
    """Whether this export asks for the pre-octilinear geometry anywhere.
    ``storyboard`` is a name or the beats themselves."""
    if view == GEO_VIEW:
        return True
    if view is None and preset is not None and preset.view == GEO_VIEW:
        return True
    board = storyboard or (preset.storyboard if preset else "")
    return any(_view_of(b) == GEO_VIEW for b in _beats_of(board))


def check_geographic(key: str, *, view: str | None = None,
                     preset: Preset | None = None,
                     storyboard: str | Sequence[Beat | dict] | None = None) -> None:
    """Refuse a geographic export of a feed that has no geographic geometry.

    The page degrades quietly here -- with nothing to raise, it simply shows the
    schematic map -- so the export would come out looking dull rather than
    broken, and only on review. Better to say which switch is off.
    """
    if not wants_geographic(view=view, preset=preset, storyboard=storyboard):
        return
    if feeds.get(key).geographic:
        return
    raise ValueError(
        f"{key!r} carries no geographic geometry, so the geographic view would "
        f"silently render as the schematic map. Set geographic=True on its "
        f"Feed in feeds.py and rebuild it (bin/build-site, or "
        f"schematic.site.export()); it is off by default because it is a "
        f"second copy of every track. Feeds that have it: "
        + ", ".join(sorted(k for k, f in feeds.all().items() if f.geographic)))


VIEW_PHRASE = {
    GEO_VIEW: "where its track actually runs",
    "map": "straightened onto a 45-degree grid",
    "linear": "with every line pulled out into its own row of evenly spaced stations",
    "time": "as a time chart, every train a diagonal",
}


def storyboard_views(storyboard: str | Sequence[Beat | dict]) -> str:
    """The views a storyboard visits, in order, as one readable field. Given
    a name or the beats themselves, so a list a client wrote is described the
    way a name is."""
    return " -> ".join(_views_visited(storyboard))


def storyboard_alt(key: str, storyboard: str | Sequence[Beat | dict], *, stations: int = 0,
                   lines: int = 0) -> str:
    """Alt text for a clip that passes through several views.

    A storyboard video described as its preset's single view is simply wrong --
    "schematic map of..." for a clip that opens on geography and ends on a
    chart. The views it visits are the description, so a name and its own
    beats read the same, and a list a client wrote is never given a name.
    """
    seen = _views_visited(storyboard)
    if len(seen) < 2:
        return alt_text(key, seen[0] if seen else "map", stations=stations, lines=lines)

    feed = feeds.get(key)
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
    feed = feeds.get(key)
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
    return sorted(set(re.findall(r'data-line="([^"]+)"', svg.read_text(encoding="utf-8"))))


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
    # UTF-8 and forgiving, not text=True's locale codec: a console line the
    # recorder prints after a good capture must not raise while being read.
    proc = subprocess.run(["node", str(RECORDER), json.dumps(job)],
                          capture_output=True, encoding="utf-8", errors="replace")
    # Shown in the console's own codec with a replacement for what it cannot
    # hold: a strict code page (Windows, redirected to a file) would otherwise
    # raise on a line the recorder wrote. A UTF-8 terminal sees it unchanged.
    codec = getattr(sys.stdout, "encoding", None) or "utf-8"
    for line in (proc.stdout + proc.stderr).splitlines():
        if line.strip():
            print(("  " + line).encode(codec, "replace").decode(codec), flush=True)
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


def _ffmpeg(args: list[str], *, progress: Progress | None = None,
            frames: int | None = None, stage: str = "encode") -> None:
    """Run one ffmpeg command, the way ``loom.execute`` runs a tool: attached to
    the caller's job, so a cancel ends it and the partial file can go; stderr
    streamed to the job's log; and, given ``frames``, ffmpeg's own frame count
    reported as progress."""
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-nostats"]
    if progress is not None:
        cmd += ["-progress", "pipe:1"]
    cmd += args
    job = loom.current_job()
    if job is not None and job.cancelled:
        raise loom.Cancelled("ffmpeg: cancelled before it started")
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if job is not None and job._attach(proc):
        proc.kill()
    assert proc.stdout is not None and proc.stderr is not None
    tail: list[str] = []

    def pump_stderr() -> None:
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").rstrip("\r\n")
            tail.append(line)
            if len(tail) > 15:
                del tail[0]
            if job is not None:
                job.log(line)

    pump = threading.Thread(target=pump_stderr, daemon=True)
    pump.start()
    for raw in iter(proc.stdout.readline, b""):
        line = raw.decode(errors="replace").strip()
        if progress is not None and frames and line.startswith("frame="):
            try:
                done = int(line[6:])
            except ValueError:
                continue
            progress(stage, min(done / frames, 1.0), f"{min(done, frames)} of {frames} frames")
    proc.wait()
    pump.join()
    if job is not None:
        job._detach()
        if job.cancelled:
            raise loom.Cancelled("ffmpeg: cancelled")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}):\n" + "\n".join(tail))


def _still_quality(preset: Preset) -> list[str]:
    """What a still's encoder is told besides its size: a JPEG's quality, the
    one setting either way of making a still (``_resample``, ``_transcode``)
    shares, so the same capture is the same picture whichever made it."""
    return ["-q:v", "3"] if preset.fmt == "jpg" else []


def _resample(src: Path, dest: Path, preset: Preset) -> None:
    """Down to the preset's exact size. Lanczos, because these maps are mostly
    one-pixel strokes and a box filter turns them to mush."""
    _ffmpeg(["-i", str(src), "-vf", f"scale={preset.width}:{preset.height}:flags=lanczos",
             *_still_quality(preset), str(dest)])


# What a still's first bytes say it is, for the two formats a still preset can ask for.
_STILL_MAGIC = (("png", b"\x89PNG\r\n\x1a\n"), ("jpg", b"\xff\xd8\xff"))


def _still_format(path: Path) -> str | None:
    """A still's format from its own first bytes, never from its name: the
    defect this answers is a file labelled one thing and holding another, and
    a client's name for what it captured is a label like any other. ``None``
    when it is neither a PNG nor a JPEG."""
    with open(path, "rb") as f:
        head = f.read(8)
    for fmt, magic in _STILL_MAGIC:
        if head.startswith(magic):
            return fmt
    return None


def _transcode(src: Path, dest: Path, preset: Preset) -> None:
    """A still into the preset's format at the size it was captured: the call
    ``_resample`` makes, without its ``scale``, because ``keep`` is the plan
    saying the capture's pixels are the deliverable's."""
    _ffmpeg(["-i", str(src), *_still_quality(preset), str(dest)])


def _encode(frames: Path, dest: Path, preset: Preset, *, fade: float = 0.0,
            crf: int = 20, keep: bool = False, progress: Progress | None = None) -> None:
    """PNG sequence to a deliverable. Text never enters here.

    This ffmpeg has no drawtext, no subtitles and no freetype, so it cannot
    render a glyph. Every word in an export is drawn by the page.
    """
    src = str(frames / "%06d.png")
    n = len(list(frames.glob("*.png")))
    if preset.fmt == "gif":
        # Two passes: a palette built from the actual frames, then applied.
        # A single pass would quantise to the default 216-colour cube and the
        # agency line colours would shift. The palette goes to a directory of
        # its own: the frames are the caller's, and nothing is written there.
        with tempfile.TemporaryDirectory(prefix="legible-palette-") as tmp:
            palette = Path(tmp) / "palette.png"
            _ffmpeg(["-framerate", str(preset.fps), "-i", src,
                     "-vf", "palettegen=stats_mode=diff", str(palette)],
                    progress=progress, frames=n, stage="palette")
            _ffmpeg(["-framerate", str(preset.fps), "-i", src, "-i", str(palette), "-lavfi",
                     f"scale={preset.width}:{preset.height}:flags=lanczos[s];"
                     "[s][1:v]paletteuse=dither=bayer:bayer_scale=3", str(dest)],
                    progress=progress, frames=n)
        return

    dur = n / preset.fps
    vf = ["format=yuv420p"]
    if not keep:
        vf.insert(0, f"scale={preset.width}:{preset.height}:flags=lanczos")
    if fade > 0:
        vf = [f"fade=t=in:st=0:d={fade}",
              f"fade=t=out:st={max(dur - fade, 0):.2f}:d={fade}"] + vf
    _ffmpeg([
        "-framerate", str(preset.fps), "-i", src,
        # Several platforms mishandle a video with no audio stream at all, and
        # give no useful error when they do.
        "-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-vf", ",".join(vf),
        "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", str(crf),
        "-r", str(preset.fps), "-c:a", "aac", "-b:a", "96k", "-shortest",
        "-movflags", "+faststart", str(dest)], progress=progress, frames=n)


def _vector(key: str, dest: Path, preset: Preset) -> list[Path]:
    """Theme-paired SVG, straight from the built map. No browser involved."""
    source = MAPS_DIR / f"{key}.svg"
    if not source.exists():
        raise FileNotFoundError(f"{source} is missing; run bin/build-site first")
    svg = source.read_text(encoding="utf-8")
    out = []
    for theme, palette in PALETTES.items():
        path = dest / f"{key}-{theme}.svg"
        path.write_text(resolve(svg, palette), encoding="utf-8", newline="\n")
        out.append(path)
    return out


# The capture's supersampling and the encode's quality, by name. `scale`
# supersamples the capture; `keep` decides whether the extra pixels are
# delivered or spent on resampling. A platform that expects 1080 wide does
# better with a clean 1080 than with a 2160 it downscales itself -- and these
# maps are full of 1px strokes, which is exactly what a bad downscale ruins.
# "high" keeps them, for print and retina.
QUALITY = {
    "draft": (1, True, 26),
    "standard": (2, False, 20),
    "high": (2, True, 16),
}


@dataclass(frozen=True)
class CaptureJob:
    """One export, described: what the recorder captures and what the encode
    makes of it. The capture half is the shape ``bin/_record.js`` receives
    (``recorder_job``); the rest is what ``encode`` needs afterwards, so one
    object carries an export from the page's address to the file."""

    key: str
    preset: str
    mode: str                          # still | video
    url: str
    width: int
    height: int
    scale: int
    fps: int
    format: str                        # png | jpg | mp4 | gif
    settle: int                        # milliseconds, clock stopped
    beats: tuple[dict, ...]            # empty for a still
    keep: bool
    crf: int
    fade: float
    stem: str
    theme: str
    view: str
    storyboard: str                    # its name; CUSTOM for a list; "" for a still
    at: float | None = None            # the clock, in seconds, a still is taken at
    notes: tuple[str, ...] = ()        # what a person should hear before the capture
    caption: str | None = None         # as given; the page draws it as text
    # The corner resolved, carried even where the clock is off and the
    # address names none.
    clock_corner: str = DEFAULT_CORNER

    @property
    def filename(self) -> str:
        return f"{self.stem}.{self.format}"

    def to_dict(self) -> dict:
        """JSON-ready, beats as lists. What ``export.plan`` answers over the protocol."""
        out = asdict(self)
        out["beats"] = list(self.beats)
        out["notes"] = list(self.notes)
        return out

    def recorder_job(self, *, frames: Path | None = None, out: Path | None = None) -> dict:
        """The job ``bin/_record.js`` takes, with where it should write."""
        job = {"url": self.url, "width": self.width, "height": self.height,
               "scale": self.scale, "fps": self.fps, "format": self.format,
               "settle": self.settle, "mode": self.mode, "at": self.at}
        if self.mode == "video":
            job["beats"] = list(self.beats)
            job["frames"] = str(frames)
        else:
            job["out"] = str(out)
        return job


def plan(key: str, preset_name: str, *, theme: str = "dark", view: str | None = None,
         labels: bool | None = None, title: bool = True, clock: bool | None = None,
         at: str | None = None, lines: tuple[str, ...] = (),
         storyboard: str | Sequence[Beat | dict] | None = None, quality: str = "standard",
         fade: float = 0.0, safe: bool = False, tag: str = "", page: str | None = None,
         date: str | None = None, caption: str | None = None,
         clock_corner: str | None = None) -> CaptureJob:
    """Describe an export without doing any of it.

    Pure: reads the registry and the atlas's data, touches no file, starts no
    browser, and knows nothing of the recorder. A vector preset is not a
    capture and is refused; ``run`` handles it. ``page`` is the page's own
    address when it is not the site's file, and ``date`` the service day its
    title names, when the caller knows them; the desktop app knows both.

    ``storyboard`` is a storyboard's name or a list of beats, which
    ``authored_beats`` checks; a still ignores it. For a video, ``view`` and
    ``at`` open a named storyboard: its first beat takes them with no
    transition, so the page's address and frame 0 agree (issue 31). Beside a
    list they are refused, since the list's first beat is where they go.

    ``caption`` is drawn under the title as given (``check_caption``).
    ``clock_corner`` left out is bottom right, where the clock has always
    sat, except on a preset with safe zones, where it is top right: there
    the platform's own interface covers the bottom right, so that corner is
    refused and the bottom left comes with a note. A preset with safe zones
    carries its row on every address (issue 40).
    """
    if key not in feeds.all():
        raise KeyError(f"unknown feed {key!r}")
    if preset_name not in PRESETS:
        raise KeyError(f"unknown preset {preset_name!r}")
    preset = PRESETS[preset_name]
    if preset.kind == "vector":
        raise ValueError(f"{preset_name} is a vector preset: nothing to capture")
    if quality not in QUALITY:
        raise ValueError(f"quality is draft, standard or high, not {quality!r}")
    if caption is not None:
        check_caption(caption)
    zones = SAFE_ZONES[preset.zones] if preset.zones else None
    corner, corner_note = _clock_corner(preset, zones, clock_corner,
                                        clock=preset.kind == "video" if clock is None else clock,
                                        title=title, caption=bool(caption))
    # The beats a video plays, settled before anything reads `view` or `at`:
    # where a video opens is its first beat, and the address says the same.
    board, planned = "", ()
    if preset.kind == "video":
        if storyboard is None or isinstance(storyboard, str):
            board = storyboard or preset.storyboard
            if board not in STORYBOARDS:
                raise KeyError(f"unknown storyboard {board!r}")
            planned = STORYBOARDS[board]
            if view or at:
                first = planned[0]
                planned = (replace(first, view=view or first.view, at=at or first.at,
                                   tween=0),) + planned[1:]
        elif view or at:
            raise ValueError(BESIDE_A_LIST)
        else:
            board, planned = CUSTOM, authored_beats(storyboard)
        if board == CUSTOM or view or at:
            view, at = planned[0].view, planned[0].at
    # A still's storyboard is ignored, and a name judged as it always was.
    check_geographic(key, view=view, preset=preset,
                     storyboard=planned or (storyboard if isinstance(storyboard, str) else None))
    scale, keep, crf = QUALITY[quality]
    stem = (f"{key}-{preset.name}" + (f"-{theme}" if theme != "dark" else "")
            + (f"-{tag}" if tag else ""))
    url = url_for(key, preset, view=view, labels=labels, title=title, clock=clock,
                  theme=theme, at=at, lines=lines, safe=safe, page=page, date=date,
                  caption=caption, clock_corner=corner, zones=zones)
    beats: tuple[dict, ...] = ()
    notes: list[str] = []
    if preset.kind == "video":
        # A sweep faster than this stops reading as motion: a train that lives
        # 2,000 seconds appears in a handful of frames and jumps between them.
        # Worth saying before spending a minute capturing it.
        for b in planned:
            if b.sweep:
                rate = sweep_rate(b, preset.fps, (0.0, 86_400.0))
                if rate > READABLE_SWEEP:
                    notes.append(f"this sweep advances {rate:.0f} simulated seconds per "
                                 f"frame, so trains will jump rather than move. Narrow "
                                 f"the beat's span, or lengthen it.")
        # The clock bounds are the feed's, so a sweep with no explicit span
        # covers whatever service day this network actually has.
        beats = tuple(beat_payload(planned))
    if corner_note:
        notes.append(corner_note)
    # A video's beats pin the clock themselves; a still is pinned here.
    pinned = _hms(at) if at else (_hms(PAGE_START) if preset.kind == "still" else None)
    return CaptureJob(key=key, preset=preset.name, mode=preset.kind, url=url,
                      width=preset.width, height=preset.height, scale=scale,
                      fps=preset.fps, format=preset.fmt, settle=SETTLE_MS, beats=beats,
                      keep=keep, crf=crf, fade=fade, stem=stem, theme=theme,
                      view=view or preset.view, storyboard=board, at=pinned,
                      notes=tuple(notes), caption=caption, clock_corner=corner)


def check_caption(caption: object) -> str:
    """A caption as the page will draw it, or a ``ValueError`` naming the bound.

    Counted in code points, as Python's ``len`` counts. Never trimmed and never
    escaped here: the page sets it as text, so ``<b>`` is drawn as those three
    characters.
    """
    bound = f"A caption is 1 to {CAPTION_MAX} characters on one line"
    if not isinstance(caption, str):
        raise ValueError(f"{bound}; this one is not text.")
    if any(mark in caption for mark in LINE_BREAKS):
        raise ValueError(f"{bound}; this one has a line break.")
    if not 1 <= len(caption) <= CAPTION_MAX:
        raise ValueError(f"{bound}; this one is {len(caption)}.")
    return caption


def _clock_corner(preset: Preset, zones: Zones | None, asked: str | None, *,
                  clock: bool, title: bool, caption: bool) -> tuple[str, str]:
    """The corner the clock takes, and a note for a person, or a refusal.

    Judged only where the clock is drawn; a still without one still resolves
    a corner, which its plan carries. The title and a caption are the name
    block, which sits top left, so a clock there would be drawn over them.
    """
    corner = asked if asked is not None else ("top-right" if zones else DEFAULT_CORNER)
    if corner not in CLOCK_CORNERS:
        raise ValueError(f"Choose the clock's corner from {', '.join(CLOCK_CORNERS)}; "
                         f"{corner!r} is not one of them.")
    if not clock:
        return corner, ""
    if zones and corner == "bottom-right":
        what = "button rail" if zones.rail_width is not None else "bottom zone"
        raise ValueError(f"Choose another corner for the clock: on {preset.name}, "
                         f"{preset.platform}'s {what} covers the bottom right.")
    if corner == "top-left" and (title or caption):
        named = ("title and the caption sit" if title and caption
                 else "title sits" if title else "caption sits")
        raise ValueError(f"Choose another corner for the clock: the {named} top left.")
    if zones and corner == "bottom-left" and zones.bottom is not None:
        return corner, (f"the clock sits bottom left, inside {preset.platform}'s bottom "
                        f"zone (the lowest {zones.bottom:.0%} of the frame), where "
                        f"{preset.platform}'s own interface can cover it; top right "
                        f"keeps it clear.")
    return corner, ""


def capture(job: CaptureJob, *, frames: Path | None = None, out: Path | None = None) -> Path:
    """Run the recorder for a job: a video's frames into ``frames``, a still to
    ``out``. Returns where it wrote. The one function here that knows the
    recorder exists; the desktop app captures for itself (its ADR-024) and
    never calls this."""
    if job.mode == "video":
        if frames is None:
            raise ValueError("a video capture needs a frames directory")
        frames.mkdir(parents=True, exist_ok=True)
        _run_recorder(job.recorder_job(frames=frames))
        return frames
    if out is None:
        raise ValueError("a still capture needs an output path")
    out.parent.mkdir(parents=True, exist_ok=True)
    _run_recorder(job.recorder_job(out=out))
    return out


def sidecar_path(path: Path) -> Path:
    """Where a deliverable's sidecar sits: beside it, with .json appended."""
    return path.with_suffix(path.suffix + ".json")


def encode(job: CaptureJob, source: Path, dest: Path, *, provenance: dict | None = None,
           progress: Progress | None = None) -> list[Path]:
    """What was captured -- a directory of frames, or one still -- to the
    deliverable at ``dest``, with its sidecar beside it. ``source`` is the
    caller's: read, never written to, never removed. ``provenance`` is what
    the caller knows about the map that the atlas's data would not (the
    desktop app's project has its own service day and diagnostics); it goes
    into the sidecar over the atlas's. A cancel, a failure or a file over the
    platform's limit leaves nothing behind. Returns what it wrote."""
    preset = PRESETS[job.preset]
    _guard_outside_repo(dest.parent)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        if job.mode == "video":
            _encode(source, dest, preset, fade=job.fade, crf=job.crf, keep=job.keep,
                    progress=progress)
        elif job.keep:
            # Kept as captured, when it is already what the preset delivers. A
            # client that captures for itself (the desktop app writes PNG) can
            # hand over a format the preset is not, and a copy would put those
            # bytes under the preset's extension: a .jpg that is a PNG, or a
            # PNG over the platform's limit that a JPEG would have met (issue
            # 30). Then it is transcoded, at the size it was captured.
            if _still_format(source) == preset.fmt:
                shutil.copyfile(source, dest)
            else:
                _transcode(source, dest, preset)
        else:
            _resample(source, dest, preset)
        check_size(dest, preset)
        _write_sidecar(job.key, preset, [dest], theme=job.theme, view=job.view,
                       storyboard=job.storyboard, beats=job.beats, provenance=provenance,
                       alt=(provenance or {}).get("alt"))
    except BaseException:
        for path in (dest, sidecar_path(dest)):
            path.unlink(missing_ok=True)
        raise
    if progress is not None:
        progress("encode", 1.0, dest.name)
    return [dest]


def preset_table() -> list[dict]:
    """Every preset as data, for a client that offers them."""
    return [{"name": p.name, "platform": p.platform, "width": p.width, "height": p.height,
             "kind": p.kind, "format": p.fmt, "view": p.view, "labels": p.labels,
             "storyboard": p.storyboard or None, "fps": p.fps, "max_bytes": p.max_bytes,
             "frame_top": p.frame_top, "safe_zones": p.safe_zones, "note": p.note}
            for p in PRESETS.values()]


def storyboard_table() -> list[dict]:
    """Every storyboard as data: its beats as written, and what they add up to."""
    return [{"name": name,
             "views": storyboard_views(name),
             "seconds": sum(b.secs for b in beats),
             "geographic": wants_geographic(storyboard=name),
             "beats": [{"secs": b.secs, "view": b.view, "labels": b.labels, "at": b.at,
                        "speed": b.speed, "sweep": b.sweep, "hours": b.hours,
                        "span": list(b.span) if b.span else None, "tween": b.tween}
                       for b in beats]}
            for name, beats in STORYBOARDS.items()]


def run(key: str, preset_name: str, *, theme: str = "dark", view: str | None = None,
        labels: bool | None = None, title: bool = True, clock: bool | None = None,
        at: str | None = None, lines: tuple[str, ...] = (),
        storyboard: str | None = None, quality: str = "standard", fade: float = 0.0,
        safe: bool = False, tag: str = "", out: Path | None = None) -> list[Path]:
    """Export one network for one destination. Returns what it wrote.

    ``plan``, then ``capture``, then ``encode``, in a working directory that
    lives only as long as the export; a vector preset needs no browser and is
    resolved straight from the built map. ``clock`` and ``title`` are the two
    pieces of furniture the page draws over the map; both off is the essay's
    own figure, which carries neither. ``tag`` goes into the filename, so two
    dressings of the same preset -- with the name and without it -- can sit in
    one folder instead of overwriting each other.
    """
    if key not in feeds.all():
        raise KeyError(f"unknown feed {key!r}")
    preset = PRESETS[preset_name]
    check_geographic(key, view=view, preset=preset, storyboard=storyboard)
    dest = desktop_dir(key, out)
    _guard_outside_repo(dest)

    if preset.kind == "vector":
        written = _vector(key, dest, preset)
        for path in written:
            check_size(path, preset)
        _write_sidecar(key, preset, written, theme=theme, view=view or preset.view)
        return written

    job = plan(key, preset_name, theme=theme, view=view, labels=labels, title=title,
               clock=clock, at=at, lines=lines, storyboard=storyboard, quality=quality,
               fade=fade, safe=safe, tag=tag)
    for note in job.notes:
        print(f"  note: {note}")
    with tempfile.TemporaryDirectory(prefix="legible-capture-") as tmp:
        work = Path(tmp)
        if job.mode == "video":
            source = capture(job, frames=work / "frames")
        else:
            source = capture(job, out=work / f"capture.{job.format}")
        return encode(job, source, dest / job.filename)


def _write_sidecar(key: str, preset: Preset, written: list[Path], *,
                   theme: str, view: str, storyboard: str = "",
                   beats: Sequence[Beat | dict] = (),
                   provenance: dict | None = None,
                   alt: str | None = None) -> None:
    """What this file is, beside the file. Includes the caveats the atlas shows:
    an image travels further than the page it came from. ``provenance`` from
    the caller wins over the atlas's, field by field. ``alt`` is the caller's own
    description, written as given in place of the generated one; it is not
    checked here, the server checks it."""
    feed = feeds.get(key)
    prov = {**_provenance(key),
            **{k: v for k, v in (provenance or {}).items() if v is not None}}
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
            "view": storyboard_views(beats or storyboard) or view,
            "storyboard": storyboard or None,
            "theme": theme,
            "alt": alt if alt is not None else (
                storyboard_alt(key, beats or storyboard, **stats) if storyboard
                else alt_text(key, view, **stats)),
            "service_date": prov.get("service_date"),
            "trips": prov.get("trips"),
            # What the atlas says about this network, carried with the picture.
            "caveats": prov.get("caveats", []),
            "notes": list(feed.notes),
            # A person's own feed without its secrets: the file sits beside
            # a picture that is made to be shared, and a feed they added can
            # come from a keyed link. A preset's is public, and written whole.
            "source": feed.shown_url,
        }
        sidecar_path(path).write_text(json.dumps(meta, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")


def _provenance(key: str) -> dict:
    """The atlas's own numbers and caveats for this network.

    Read from the generated networks.json rather than recomputed: it is already
    the single source the site uses, and an image travels further than the page
    it came from, so whatever the atlas admits should travel with it.
    """
    path = REPO_ROOT / "site" / "src" / "_data" / "networks.json"
    if not path.exists():
        return {}
    for entry in json.loads(path.read_text(encoding="utf-8")).get("networks", []):
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
    text = svg.read_text(encoding="utf-8")
    return {"stations": text.count("<circle"),
            "lines": len(set(re.findall(r'data-line="([^"]+)"', text)))}


def poster(video: Path, dest: Path, at: float = 0.6) -> Path:
    """A representative frame, for a contact sheet or a video cover."""
    _ffmpeg(["-ss", f"{at * _duration(video):.2f}", "-i", str(video),
             "-frames:v", "1", "-q:v", "2", str(dest)])
    return dest


def _duration(path: Path) -> float:
    out = subprocess.run([ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True,
                         encoding="utf-8", errors="replace")
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
        sheet.write_text("\n".join(out), encoding="utf-8", newline="\n")
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
    # A still of a video preset, at 1x: the plan's shape with the mode changed.
    job = replace(plan(key, preset.name, theme=theme, view=view, safe=True, quality="draft"),
                  mode="still", format="png", beats=(), storyboard="", notes=(),
                  at=_hms(PAGE_START))
    capture(job, out=path)
    return [path]
