"""The engine as a JSON-RPC 2.0 server over stdin and stdout.

    python -m schematic.serve            # serve until stdin closes or engine.shutdown
    python -m schematic.serve --schema   # print the protocol's JSON Schema

The transport is the Language Server Protocol's: ``Content-Length`` framed
messages on stdin and stdout, stderr for logs. Long requests stay open until
they finish; while they run, ``job/progress`` and ``job/log`` notifications
carry the request's id, and ``$/cancelRequest`` ends the process the request
is waiting on (a LOOM tool, or ffmpeg), or stops a feed it is downloading
between chunks and caches nothing, and answers it with the cancelled error.
Work that waits on neither (``feeds.service`` reading a calendar) runs to its
end and is answered with the same error. ``feeds.remove`` has a point of no
return, the registry's write: a cancel before it leaves the feed registered
with every file in place and is answered with the cancelled error; a cancel
after it is not honoured, since the feed is already forgotten and its files
are removed to the end, and the answer says the cancel came too late.

Errors a person can act on are code -32000 with ``data: {kind, detail,
hint}``: ``hint`` is the sentence the engine already raises, ``detail`` says
where. Bad parameters are -32602 with the same shape and ``kind: "params"``.

``protocol/v1.json`` is the contract; the desktop app generates its types
from it. A change here is a change there.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import platform
import re
import shutil
import sys
import traceback
from importlib import resources
from pathlib import Path
from typing import IO, Any, Callable

from pylsp_jsonrpc.endpoint import Endpoint
from pylsp_jsonrpc.exceptions import (JsonRpcException, JsonRpcInvalidParams,
                                      JsonRpcRequestCancelled)
from pylsp_jsonrpc.streams import JsonRpcStreamReader, JsonRpcStreamWriter

from . import (__version__, config, diagnostics, export, feeds, loom, pipeline, schedule,
               thumbnail)
from .crs import to_mercator
from .describe import station_name
from .linegraph import LineGraph
from .render import (HEX_COLOR_PATTERN, draw_stage, octilinearity, stage as render_stage,
                     summary as render_summary)
from .render import PRESETS, STYLE_RANGES, Style, preset_style

log = logging.getLogger(__name__)

PROTOCOL = 1
ENGINE_ERROR = -32000

# The same patterns as protocol/v1.json; test_serve checks they agree.
KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DATE_PATTERN = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
CLOCK_PATTERN = re.compile(export.CLOCK)
URL_PATTERN = re.compile(r"^[a-z][a-z0-9+.-]*://[^\s]+$")
STEM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# A line's display name: no line break of any kind, the two Unicode ones
# included, since the page writes it into a chip, a row and a train's title.
LINE_NAME_PATTERN = re.compile(r"^[^\r\n\u2028\u2029]*$")
LINE_NAME_LENGTH = 40
# No line break of any kind. Matched whole (fullmatch): `$` alone would let a
# trailing newline through.
CAPTION_PATTERN = re.compile(r"^[^\r\n\u2028\u2029]*$")

# An exception's kind is the module that raised it, since that is where the
# sentence for a person was written.
KIND_BY_MODULE = {"feeds": "feed", "pipeline": "feed", "schedule": "schedule",
                  "export": "export", "loom": "loom"}
# Every kind the protocol names: the modules', the transport's, and the one
# for a layout named that is not stored (nothing ran, nothing failed).
KINDS = sorted(set(KIND_BY_MODULE.values()) | {"engine", "io", "params", "layout"})


class _Job(loom.Job):
    """A job that can pass its point of no return: work that has done what a
    cancel could no longer undo says so with ``commit()``, and from then on a
    cancel is not honoured and does not turn the work's own answer, or its own
    failure, into the cancelled error. Only the worker reads or sets it."""

    def __init__(self, log: Callable[[str], None] | None = None) -> None:
        super().__init__(log=log)
        self.committed = False

    def commit(self) -> None:
        self.committed = True


Progress = pipeline.Progress
Work = Callable[[_Job, Progress], Any]


def schema() -> dict[str, Any]:
    """The protocol's JSON Schema, as shipped in the package."""
    text = resources.files("schematic.protocol").joinpath("v1.json").read_text(encoding="utf-8")
    return json.loads(text)


# ---------------------------------------------------------------------- errors

class EngineError(JsonRpcException):
    """Code -32000: something a person can act on, with the sentence to show them."""

    def __init__(self, kind: str, hint: str, detail: str | None = None, **more: Any) -> None:
        super().__init__(message=hint, code=ENGINE_ERROR,
                         data={"kind": kind, "detail": detail or hint, "hint": hint, **more})


def invalid_params(hint: str) -> JsonRpcInvalidParams:
    return JsonRpcInvalidParams(message=hint,
                                data={"kind": "params", "detail": hint, "hint": hint})


def classify(exc: BaseException) -> EngineError:
    """Turn an exception out of the pipeline into an error a client can show."""
    frames = traceback.extract_tb(exc.__traceback__)
    if isinstance(exc, pipeline.LayoutMissing):
        kind = "layout"
    elif isinstance(exc, loom.LoomError):
        kind = "loom"
    elif isinstance(exc, OSError):
        kind = "io"
    else:
        kind = "engine"
        for frame in reversed(frames):
            path = Path(frame.filename)
            if path.parent == config.PACKAGE_DIR and path.stem in KIND_BY_MODULE:
                kind = KIND_BY_MODULE[path.stem]
                break

    summary = "".join(traceback.format_exception_only(exc)).strip()
    where = f" ({Path(frames[-1].filename).name}:{frames[-1].lineno})" if frames else ""
    detail = summary + where

    if isinstance(exc, (ValueError, RuntimeError)) and str(exc):
        hint = str(exc)
    elif isinstance(exc, OSError) and exc.strerror:
        hint = f"{exc.strerror}: {exc.filename}" if exc.filename else exc.strerror
    else:
        hint = "The engine hit an error it did not expect; the detail says where."
    return EngineError(kind, hint, detail)


# ------------------------------------------------------------------ parameters

def _object(method: str, params: Any) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise invalid_params(f"{method} takes an object of parameters")
    return dict(params)


def _no_params(method: str, params: Any) -> None:
    if params not in (None, {}):
        raise invalid_params(f"{method} takes no parameters")


def _no_extra(method: str, left: dict[str, Any]) -> None:
    if left:
        raise invalid_params(f"{method} does not take {', '.join(sorted(left))}")


def _feed_key(value: Any) -> str:
    if not isinstance(value, str) or not KEY_PATTERN.fullmatch(value):
        raise invalid_params("key must be a feed key: lower-case letters, digits and hyphens")
    if value not in feeds.all():
        raise EngineError("feed", f"{value!r} is not a registered feed")
    return value


def _date(value: Any, name: str = "date") -> dt.date:
    if value is None:
        if name == "date":
            raise invalid_params(
                "date is required: the service day to draw, as YYYY-MM-DD. The engine "
                "never picks one, because its choice would depend on the day you asked.")
        raise invalid_params(f"{name} must be a calendar day as YYYY-MM-DD, or left out")
    if not isinstance(value, str) or not DATE_PATTERN.fullmatch(value):
        raise invalid_params(f"{name} must be a calendar day as YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise invalid_params(f"{name}: {value} is not a calendar day") from None


def _token(value: Any) -> str:
    if not isinstance(value, str) or not TOKEN_PATTERN.fullmatch(value):
        raise invalid_params("out must be a folder name: letters, digits, dot, underscore "
                             "and hyphen, not starting with a dot, never a path")
    return value


# LOOM's -m: one or more of the names it knows, comma-joined, or route_types.
MODE_PATTERN = re.compile(r"^[a-z0-9-]+(,[a-z0-9-]+)*$")
LAYOUT_PATTERN = re.compile(pipeline.LAYOUT_ID_PATTERN)


def _regex(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        re.compile(value)
    except re.error:
        return False
    return True


def _overrides(left: dict[str, Any]) -> dict[str, Any]:
    """What a build may change about the registry entry (``feeds.OVERRIDES``)."""
    out: dict[str, Any] = {}
    mode = _optional(left, "mode",
                     lambda v: isinstance(v, str) and bool(MODE_PATTERN.fullmatch(v))
                     and feeds.valid_mode(v),
                     "must be what gtfs2graph -m takes: names such as tram, subway or rail, "
                     "or route_type numbers, comma-joined")
    if mode is not None:
        out["mode"] = mode
    agency = _optional(left, "agency", lambda v: isinstance(v, str) and len(v) <= 64,
                       "must be an agency_id from the feed, or empty for every operator")
    if agency is not None:
        out["agency"] = agency
    for name in ("label_pattern", "label_strip"):
        pattern = _optional(left, name, _regex, "must be a regular expression")
        if pattern is not None:
            out[name] = pattern
    return out


def _layout(value: Any) -> str:
    if value is None:
        raise invalid_params("layout is required: the id graph.build answered with. A map is "
                             "drawn from a stored layout and never lays one out itself.")
    if not isinstance(value, str) or not LAYOUT_PATTERN.fullmatch(value):
        raise invalid_params("layout must be a layout id: 64 hex digits, as graph.build reports")
    return value


def _flag(left: dict[str, Any], name: str) -> bool:
    value = left.pop(name, False)
    if not isinstance(value, bool):
        raise invalid_params(f"{name} must be true or false")
    return value


def _positive(left: dict[str, Any], name: str, default: float) -> float:
    value = left.pop(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise invalid_params(f"{name} must be a number above zero")
    return float(value)


def _strings(left: dict[str, Any], name: str) -> list[str] | None:
    value = left.pop(name, None)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(s, str) for s in value):
        raise invalid_params(f"{name} must be a list of strings")
    return value


COLOR_PATTERN = re.compile(HEX_COLOR_PATTERN)


def _color(left: dict[str, Any], name: str) -> str | None:
    """A colour the client chose, written ``#rrggbb``, or none."""
    value = left.pop(name, None)
    if value is None:
        return None
    if not isinstance(value, str) or not COLOR_PATTERN.fullmatch(value):
        raise invalid_params(f"{name} must be a colour written #rrggbb")
    return value


def _colors(left: dict[str, Any], name: str) -> dict[str, str] | None:
    """Line label to colour, every colour written ``#rrggbb``, or none."""
    value = left.pop(name, None)
    if value is None:
        return None
    if not isinstance(value, dict) or not all(
            isinstance(k, str) and isinstance(v, str) and COLOR_PATTERN.fullmatch(v)
            for k, v in value.items()):
        raise invalid_params(f"{name} must be an object of line label to a colour "
                             f"written #rrggbb")
    return value


def _lines(left: dict[str, Any]) -> dict[str, dict[str, Any]] | None:
    """Line label to what a client chose for that line, or none.

    Each value is an object of two optional fields and nothing else: ``name``,
    from 1 to ``LINE_NAME_LENGTH`` characters with no line break, and
    ``hidden``, true or false. A label the layout does not carry is not
    refused, as ``colors`` does not refuse one: it cannot be checked without
    reading the layout, and a project's choices must survive a narrower
    mode. ``pipeline.run`` ignores it."""
    value = left.pop("lines", None)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid_params("lines must be an object of line label to the line's name "
                             "and hidden")
    for label, chosen in value.items():
        if not isinstance(chosen, dict):
            raise invalid_params(f"lines[{label!r}] must be an object of name and hidden")
        rest = dict(chosen)
        if "name" in rest:
            name = rest.pop("name")
            if (not isinstance(name, str) or not 1 <= len(name) <= LINE_NAME_LENGTH
                    or not LINE_NAME_PATTERN.fullmatch(name)):
                raise invalid_params(f"lines[{label!r}].name must be from 1 to "
                                     f"{LINE_NAME_LENGTH} characters with no line break")
        if "hidden" in rest:
            if not isinstance(rest.pop("hidden"), bool):
                raise invalid_params(f"lines[{label!r}].hidden must be true or false")
        if rest:
            raise invalid_params(f"lines[{label!r}] does not take {', '.join(sorted(rest))}")
    return value


def _or(names: Any) -> str:
    """``a, b or c``: the names a refusal offers, in the table's order."""
    names = list(names)
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"


# The four colours of a style the wire accepts. The app never sends them (the
# page's theme owns the furniture); the command line and the site do.
STYLE_COLORS = ("background", "station_fill", "station_stroke_color", "label_color")


def _style(value: Any) -> Style | None:
    """The style a client asked the map to be drawn with, or none.

    None when the parameter is absent, so ``pipeline.run`` keeps its own
    default, ``Style(themed=True)``, and every output is what it was.
    Otherwise an object of optional fields: the eight numbers of
    ``render.STYLE_RANGES``, each inside its closed range, and the four
    colours of ``STYLE_COLORS``, each written ``#rrggbb``. Any other key is
    refused, ``themed``, ``label_char_width`` and ``default_line_color``
    among them: they are the engine's, not a client's (``default_color`` is
    a parameter of ``map.build`` itself).

    Or the name of a look instead of the fields: ``{"preset": "beck"}``
    resolves to exactly the eight numbers of ``render.PRESETS`` (issue 73),
    so it draws what the same numbers sent one by one draw. The name is
    exclusive: beside any other key it is refused, because which of the two
    won would be a guess, and a name that is not in the table is refused
    naming the ones that are. A preset carries no colour; the theme owns them.

    One rule the schema cannot hold: ``interchange_radius`` may not be below
    ``station_radius``, judged on the values the map would be drawn with,
    each field as given or else ``Style``'s default. So ``station_radius``
    above 6 alone is refused too, because the default ``interchange_radius``
    is 6, and a client that raises one sends the other.

    The result is built ``themed=True`` and always, as ``pipeline.run``'s
    default is: ``Style(**fields)`` alone would write literal colours where
    the page expects ``var(--map-*)``."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid_params("style must be an object")
    left = dict(value)
    if "preset" in left:
        name = left.pop("preset")
        if left:
            raise invalid_params("style.preset cannot be sent with the fields it resolves to; "
                                 "send one or the other")
        if not isinstance(name, str) or name not in PRESETS:
            raise invalid_params(f"style.preset must be {_or(PRESETS)}; style.presets "
                                 f"describes each")
        return Style(themed=True, **preset_style(name))
    fields: dict[str, Any] = {}
    for name, (low, high, unit) in STYLE_RANGES.items():
        if name not in left:
            continue
        number = left.pop(name)
        if not _number(number) or not low <= number <= high:
            raise invalid_params(
                f"style.{name} must be from {low:g} to {high:g}, "
                + (f"in {unit}" if unit else "as a multiple of line_width"))
        fields[name] = number
    for name in STYLE_COLORS:
        if name not in left:
            continue
        color = left.pop(name)
        if not isinstance(color, str) or not COLOR_PATTERN.fullmatch(color):
            raise invalid_params(f"style.{name} must be a colour written #rrggbb")
        fields[name] = color
    _no_extra("style", left)
    style = Style(themed=True, **fields)
    if style.interchange_radius < style.station_radius:
        raise invalid_params(
            f"style.interchange_radius ({style.interchange_radius:g}) must not be below "
            f"style.station_radius ({style.station_radius:g}); a field left out counts as its "
            f"default, so send both")
    return style


def _tuned(path: str, number: Any, tunable: pipeline.Tunable) -> Any:
    """A number of the tuning inside its closed range, as given; the sentence
    names its field by the path a client wrote and says what it counts."""
    if not _number(number) or not tunable.low <= number <= tunable.high:
        raise invalid_params(f"{path} must be from {tunable.low:g} to {tunable.high:g}, "
                             f"{tunable.unit}")
    return number


def _penalties(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid_params("tuning.penalties must be an object of "
                             + ", ".join(pipeline.PENALTIES))
    left = dict(value)
    fields: dict[str, Any] = {}
    for name, tunable in pipeline.PENALTIES.items():
        if name in left:
            fields[name] = _tuned(f"tuning.penalties.{name}", left.pop(name), tunable)
    _no_extra("tuning.penalties", left)
    return fields


def _tuning(value: Any) -> dict[str, Any]:
    """The tuning a client asked a layout to be made with, judged before any
    tool starts.

    Only called when the parameter is present, so ``tuning: null`` is refused
    rather than taken for ``tuning`` left out, as ``render.stage``'s ``date``
    is. An object of optional fields, LOOM's own flags by name with no slider
    mapped over them (the table is ``pipeline``'s): ``merge_distance`` and
    ``grid_size`` numbers inside their closed ranges, ``grid`` one of
    ``pipeline.GRIDS``, ``penalties`` an object of the five costs, each inside
    its range. Any other key is refused, at either level, and so is a string
    where a number goes; every sentence names its field as the client wrote it.

    The result is the fields as given. What they mean for the tools' flags,
    and for the layout's id, is ``pipeline.stages_for``'s."""
    if not isinstance(value, dict):
        raise invalid_params("tuning must be an object of LOOM's own settings, or left out")
    left = dict(value)
    fields: dict[str, Any] = {}
    if "merge_distance" in left:
        fields["merge_distance"] = _tuned("tuning.merge_distance", left.pop("merge_distance"),
                                          pipeline.MERGE_DISTANCE)
    if "grid" in left:
        grid = left.pop("grid")
        if not isinstance(grid, str) or grid not in pipeline.GRIDS:
            raise invalid_params("tuning.grid must be one of " + ", ".join(pipeline.GRIDS))
        fields["grid"] = grid
    if "grid_size" in left:
        fields["grid_size"] = _tuned("tuning.grid_size", left.pop("grid_size"),
                                     pipeline.GRID_SIZE)
    if "penalties" in left:
        fields["penalties"] = _penalties(left.pop("penalties"))
    _no_extra("tuning", left)
    return fields


# ---- the export methods' parameters: a preset, a page, the options, a plan

def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _preset(value: Any) -> str:
    if not isinstance(value, str) or value not in export.PRESETS:
        raise invalid_params("preset must be the name of an export preset; export.presets "
                             "lists them")
    return value


def _optional(left: dict[str, Any], name: str, ok: Callable[[Any], bool], sentence: str) -> Any:
    value = left.pop(name, None)
    if value is not None and not ok(value):
        raise invalid_params(f"{name} {sentence}")
    return value


def _export_options(value: Any) -> dict[str, Any]:
    """The optional dressing of an export, as ``export.plan`` takes it."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise invalid_params("options must be an object")
    left = dict(value)
    out: dict[str, Any] = {}
    view = _optional(left, "view", lambda v: v in export.VIEWS,
                     "must be one of " + ", ".join(export.VIEWS))
    if view is not None:
        out["view"] = view
    for flag in ("labels", "title", "clock", "safe"):
        v = _optional(left, flag, lambda x: isinstance(x, bool), "must be true or false")
        if v is not None:
            out[flag] = v
    theme = _optional(left, "theme", lambda v: v in ("dark", "light"), "must be dark or light")
    if theme is not None:
        out["theme"] = theme
    at = _optional(left, "at", lambda v: isinstance(v, str) and bool(CLOCK_PATTERN.fullmatch(v)),
                   "must be a clock, HH:MM")
    if at is not None:
        out["at"] = at
    lines = _strings(left, "lines")
    if lines is not None:
        out["lines"] = tuple(lines)
    # A name or a list of beats; the type first, since a list is not a key.
    board = left.pop("storyboard", None)
    if isinstance(board, list):
        try:
            out["storyboard"] = export.authored_beats(board)
        except ValueError as exc:
            raise invalid_params(str(exc)) from exc
    elif isinstance(board, str) and board in export.STORYBOARDS:
        out["storyboard"] = board
    elif board is not None:
        raise invalid_params("storyboard must be the name of a storyboard; "
                             "export.storyboards lists them")
    quality = _optional(left, "quality", lambda v: v in export.QUALITY,
                        "must be draft, standard or high")
    if quality is not None:
        out["quality"] = quality
    fade = _optional(left, "fade", lambda v: _number(v) and v >= 0, "must be seconds, 0 or more")
    if fade is not None:
        out["fade"] = float(fade)
    tag = _optional(left, "tag", lambda v: isinstance(v, str) and bool(TOKEN_PATTERN.fullmatch(v)),
                    "must be a short filename suffix: letters, digits, dot, underscore, hyphen")
    if tag is not None:
        out["tag"] = tag
    caption = _optional(left, "caption", _caption,
                        f"must be text of 1 to {export.CAPTION_MAX} characters on one line")
    if caption is not None:
        out["caption"] = caption
    corner = _optional(left, "clock_corner", lambda v: v in export.CLOCK_CORNERS,
                       "must be one of " + ", ".join(export.CLOCK_CORNERS))
    if corner is not None:
        out["clock_corner"] = corner
    _no_extra("export.plan options", left)
    return out


def _caption(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value) <= export.CAPTION_MAX
            and bool(CAPTION_PATTERN.fullmatch(value)))


def _beat_payload(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid_params(f"{where} is not an object")
    left = dict(value)
    secs = left.pop("secs", None)
    if not _number(secs) or secs <= 0:
        raise invalid_params(f"{where} lasts no time")
    out: dict[str, Any] = {"secs": secs}
    out["view"] = _optional(left, "view", lambda v: v in export.VIEWS,
                            "names a view the page lacks")
    out["labels"] = _optional(left, "labels", lambda v: isinstance(v, bool),
                              "must be true or false")
    for field in ("at", "speed", "hours", "lo", "hi", "tween"):
        out[field] = _optional(left, field, _number, "must be a number")
    sweep = left.pop("sweep", False)
    if not isinstance(sweep, bool):
        raise invalid_params(f"{where} has a sweep that is not true or false")
    out["sweep"] = sweep
    _no_extra(where, left)
    return out


def _capture_job(value: Any) -> export.CaptureJob:
    """A plan the client hands back, checked field by field before it is trusted."""
    if not isinstance(value, dict):
        raise invalid_params("plan must be the object export.plan answered with")
    left = dict(value)
    left.pop("filename", None)          # the plan's own convenience; recomputed
    key = _feed_key(left.pop("key", None))
    preset = _preset(left.pop("preset", None))
    mode = left.pop("mode", None)
    if mode not in ("still", "video"):
        raise invalid_params("plan.mode must be still or video")
    url = left.pop("url", None)
    if not isinstance(url, str) or not URL_PATTERN.fullmatch(url):
        raise invalid_params("plan.url must be the page's address")
    ints: dict[str, int] = {}
    for name, low in (("width", 1), ("height", 1), ("scale", 1), ("fps", 1), ("settle", 0),
                      ("crf", 0)):
        v = left.pop(name, None)
        if not isinstance(v, int) or isinstance(v, bool) or v < low:
            raise invalid_params(f"plan.{name} must be a whole number, {low} or more")
        ints[name] = v
    fmt = left.pop("format", None)
    if fmt not in ("png", "jpg", "mp4", "gif"):
        raise invalid_params("plan.format must be png, jpg, mp4 or gif")
    beats_raw = left.pop("beats", None)
    if not isinstance(beats_raw, list):
        raise invalid_params("plan.beats must be a list")
    beats = tuple(_beat_payload(b, f"plan.beats[{i}]") for i, b in enumerate(beats_raw))
    keep = left.pop("keep", None)
    if not isinstance(keep, bool):
        raise invalid_params("plan.keep must be true or false")
    fade = left.pop("fade", None)
    if not _number(fade) or fade < 0:
        raise invalid_params("plan.fade must be seconds, 0 or more")
    stem = left.pop("stem", None)
    if not isinstance(stem, str) or not STEM_PATTERN.fullmatch(stem):
        raise invalid_params("plan.stem must be a file name without a path or an extension")
    theme = left.pop("theme", None)
    if theme not in ("dark", "light"):
        raise invalid_params("plan.theme must be dark or light")
    view = left.pop("view", None)
    if view not in export.VIEWS:
        raise invalid_params("plan.view must be one of " + ", ".join(export.VIEWS))
    board = left.pop("storyboard", "")
    if not isinstance(board, str) or (board and board != export.CUSTOM
                                      and board not in export.STORYBOARDS):
        raise invalid_params("plan.storyboard must be a storyboard's name, custom, or empty")
    at = left.pop("at", None)
    if at is not None and not _number(at):
        raise invalid_params("plan.at must be seconds, or null")
    notes = left.pop("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
        raise invalid_params("plan.notes must be a list of strings")
    caption = left.pop("caption", None)
    if caption is not None and not _caption(caption):
        raise invalid_params(f"plan.caption must be text of 1 to {export.CAPTION_MAX} "
                             f"characters on one line, or null")
    corner = left.pop("clock_corner", export.DEFAULT_CORNER)
    if corner not in export.CLOCK_CORNERS:
        raise invalid_params("plan.clock_corner must be one of "
                             + ", ".join(export.CLOCK_CORNERS))
    _no_extra("plan", left)
    return export.CaptureJob(key=key, preset=preset, mode=mode, url=url, width=ints["width"],
                             height=ints["height"], scale=ints["scale"], fps=ints["fps"],
                             format=fmt, settle=ints["settle"], beats=beats, keep=keep,
                             crf=ints["crf"], fade=float(fade), stem=stem, theme=theme,
                             view=view, storyboard=board, at=at, notes=tuple(notes),
                             caption=caption, clock_corner=corner)


def _absolute(left: dict[str, Any], name: str) -> Path:
    """A path the client owns: absolute, and never inside the engine's own tree."""
    value = left.pop(name, None)
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise invalid_params(f"{name} must be an absolute path")
    path = Path(value)
    try:
        export._guard_outside_repo(path if name == "source" else path.parent)
    except ValueError:
        raise invalid_params(f"{name} must not be inside the engine's own repository") from None
    return path


def _provenance(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise invalid_params("provenance must be an object")
    left = dict(value)
    out: dict[str, Any] = {}
    date = left.pop("service_date", None)
    if date is not None:
        out["service_date"] = _date(date).isoformat()
    for name in ("trips", "stations", "lines"):
        v = _optional(left, name,
                      lambda x: isinstance(x, int) and not isinstance(x, bool) and x >= 0,
                      "must be a count")
        if v is not None:
            out[name] = v
    caveats = _strings(left, "caveats")
    if caveats is not None:
        out["caveats"] = caveats
    alt = left.pop("alt", None)
    if alt is not None:
        longest = 1000  # code points, which is what the schema's maxLength counts
        if not isinstance(alt, str):
            raise invalid_params("provenance.alt must be text")
        if len(alt) > longest:
            raise invalid_params(f"provenance.alt is {len(alt):,} characters; "
                                 f"it may be at most {longest:,}")
        alt = alt.strip()
        if not alt:
            raise invalid_params("provenance.alt is empty; omit it instead, and the "
                                 "sidecar keeps the description the engine writes")
        out["alt"] = alt
    _no_extra("provenance", left)
    return out


# ------------------------------------------------------------------- the server

class EngineEndpoint(Endpoint):
    """The engine's methods, plus what the base class lacks: the id of the
    request being handled, and a registry of running jobs for cancellation."""

    def __init__(self, consumer: Callable[[dict[str, Any]], None], max_workers: int = 4) -> None:
        self.jobs: dict[Any, loom.Job] = {}
        self.stopping = False
        self._request_id: Any = None
        super().__init__({
            "engine.info": self.engine_info,
            "engine.shutdown": self.engine_shutdown,
            "graph.build": self.graph_build,
            "map.build": self.map_build,
            "style.presets": self.style_presets,
            "export.presets": self.export_presets,
            "export.storyboards": self.export_storyboards,
            "export.plan": self.export_plan,
            "export.encode": self.export_encode,
            "feeds.service": self.feeds_service,
            "feeds.list": self.feeds_list,
            "feeds.add": self.feeds_add,
            "feeds.remove": self.feeds_remove,
            "feeds.inspect": self.feeds_inspect,
            "render.stage": self.render_stage,
        }, consumer, max_workers=max_workers)

    @property
    def methods(self) -> list[str]:
        return list(self._dispatcher)

    # The base class calls handlers with the params alone; the id is needed
    # for progress and cancellation, so keep it while the handler runs. Only
    # the reader thread handles requests, so one slot is enough.
    def _handle_request(self, msg_id: Any, method: str, params: Any) -> None:
        self._request_id = msg_id
        try:
            super()._handle_request(msg_id, method, params)
        finally:
            self._request_id = None

    # The base class can only cancel a job that has not started. Ours kills
    # the LOOM process; the worker then raises and the request is answered
    # with the cancelled error through the usual callback.
    def _handle_cancel_notification(self, msg_id: Any) -> None:
        job = self.jobs.get(msg_id)
        if job is None:
            log.warning("cancel for a request that is not running: %s", msg_id)
            return
        log.info("cancelling request %s", msg_id)
        job.cancel()

    def close(self) -> None:
        """Cancel whatever is running and stop the workers. No process outlives this."""
        for job in list(self.jobs.values()):
            job.cancel()
        self.shutdown()

    def _job(self, work: Work) -> Callable[[], Any]:
        """A long method: a Job registered under the request's id, run on a worker.

        The base class runs the returned callable in its pool and answers the
        request with what it returns or raises.
        """
        msg_id = self._request_id
        job = _Job(log=lambda line: self.notify(
            "job/log", {"id": msg_id, "level": "info", "line": line}))
        self.jobs[msg_id] = job

        def progress(stage: str, fraction: float, message: str) -> None:
            note = {"id": msg_id, "stage": stage, "fraction": round(fraction, 4),
                    "message": message}
            # A report of one of a layout's stages names the layout, so a
            # client can draw the stage before the run ends (E27, issue 43).
            layout = pipeline.reporting()
            if layout is not None:
                note["layout"] = layout
            self.notify("job/progress", note)

        def run() -> Any:
            try:
                if job.cancelled:
                    raise JsonRpcRequestCancelled()
                # A feed downloaded anywhere inside the work - a preset's
                # zip fetched the first time a layout, an inspection or a
                # service-day read needs it - reports its bytes as stage
                # download and stops on a cancel, leaving nothing (E36).
                with loom.cancellable(job), feeds.watched(_download_report(progress),
                                                          lambda: job.cancelled):
                    result = work(job, progress)
                # Work that starts no process cannot be interrupted, so a
                # cancel that arrived while it ran is honoured here: the
                # answer to a cancelled request is the cancelled error, never
                # a result the client has stopped waiting for. Unless the work
                # passed its point of no return, when what it did stands and
                # the answer says so.
                if job.cancelled and not job.committed:
                    raise JsonRpcRequestCancelled()
                return result
            except (loom.Cancelled, feeds.Interrupted):
                # A tool ended by the cancel, or a download stopped by it
                # (E36): the cancelled error, whatever the work was.
                raise JsonRpcRequestCancelled() from None
            except JsonRpcException:
                raise
            except Exception as exc:
                if job.cancelled and not job.committed:
                    raise JsonRpcRequestCancelled() from None
                log.exception("request %s failed", msg_id)
                raise classify(exc) from exc
            finally:
                self.jobs.pop(msg_id, None)

        return run

    # -- methods --

    def engine_info(self, params: Any = None) -> dict[str, Any]:
        _no_params("engine.info", params)
        return {
            "engine": __version__,
            "protocol": PROTOCOL,
            "python": platform.python_version(),
            "loom": {"commit": loom.commit(), "backend": loom.backend().name},
            "ffmpeg": shutil.which(export.ffmpeg_path()),
            "home": str(config.home()),
        }

    def engine_shutdown(self, params: Any = None) -> dict[str, Any]:
        _no_params("engine.shutdown", params)
        for job in list(self.jobs.values()):
            job.cancel()
        self.stopping = True
        return {"ok": True}

    def graph_build(self, params: Any) -> Callable[[], Any]:
        left = _object("graph.build", params)
        key = _feed_key(left.pop("key", None))
        force = _flag(left, "force")
        overrides = _overrides(left)
        # The tuning's flags are part of the layout's id, so a tuned layout is
        # a layout of its own; a tuning of nothing or of LOOM's defaults gives
        # the stages every earlier build ran with, and the id stored today.
        tuned = pipeline.stages_for(_tuning(left.pop("tuning")) if "tuning" in left else None)
        _no_extra("graph.build", left)

        def work(job: loom.Job, progress: Progress) -> dict[str, Any]:
            layout = pipeline.lay_out(key, force=force, stages=tuned, progress=progress,
                                      **overrides)
            paths = layout.paths
            octi = LineGraph.from_geojson(paths["octi"])
            pipeline.require_edges(layout.feed, octi)
            stages = {stage: _stage_summary(LineGraph.from_geojson(path))
                      for stage, path in paths.items()}
            ok, total = octilinearity(octi.reproject(to_mercator))
            stages["octi"]["octilinear"] = ok / total if total else 0.0
            return {"layout": layout.id, "meta": layout.meta, "stages": stages,
                    "paths": {s: str(p) for s, p in paths.items()}}

        return self._job(work)

    def map_build(self, params: Any) -> Callable[[], Any]:
        left = _object("map.build", params)
        key = _feed_key(left.pop("key", None))
        layout = _layout(left.pop("layout", None))
        date = _date(left.pop("date", None))
        out = _token(left.pop("out")) if "out" in left else None
        width = _positive(left, "width", 1800.0)
        colors = _colors(left, "colors")
        default_color = _color(left, "default_color")
        line_order = _strings(left, "line_order")
        style = _style(left.pop("style", None))
        lines = _lines(left)
        _no_extra("map.build", left)
        folder = config.out_dir() / out if out else None

        def work(job: loom.Job, progress: Progress) -> dict[str, Any]:
            # From the stored layout, and never a layout of its own: a map
            # that re-laid a network unasked would be a different map.
            result = pipeline.run(key, layout=layout, date=date, width=width,
                                  colors=colors, default_color=default_color, style=style,
                                  line_order=line_order, lines=lines, out_dir=folder,
                                  progress=progress)
            where = folder or config.out_dir()
            diag = result.diagnostics()
            # The picture the app's front door shows, beside the page, drawn
            # from the graph just drawn in the project's colours and order.
            thumbs = thumbnail.write(result.graph, where, key, colors=colors,
                                     default_color=default_color, line_order=line_order)
            return {
                "layout": result.layout,
                "date": result.date.isoformat(),
                "files": {"svg": str(where / f"{key}.svg"),
                          "html": str(where / f"{key}.html"),
                          "positions": str(where / f"{key}.positions.json"),
                          **{name: str(path) for name, path in thumbs.items()}},
                # What a client's trip pickers offer (engine issue 49): the
                # stations of the map just drawn, a hidden line's own gone.
                "stations": _stations(result.graph),
                "summary": diag.summary(),
                "diagnostics": diag.to_dict(),
                "caveats": diagnostics.caveats(diag),
                "issues": round(diagnostics.issue_score(diag), 4),
            }

        return self._job(work)

    def feeds_service(self, params: Any) -> Callable[[], Any]:
        """When a feed runs and which day to draw, from an anchor the caller
        gives (today when it does not): the same feed and anchor answer the
        same day on every machine, which is what lets a client store it. A
        long request, because reading a large feed's tables takes seconds."""
        left = _object("feeds.service", params)
        key = _feed_key(left.pop("key", None))
        anchor = _date(left.pop("anchor"), "anchor") if "anchor" in left else dt.date.today()
        lines = _strings(left, "lines")
        _no_extra("feeds.service", left)

        def work(_job: loom.Job, _progress: Progress) -> dict[str, Any]:
            # The calendar and the trips are all the day needs; stop_times,
            # the bulk of a large feed, is left unread.
            tables = feeds.tables(key, only=schedule.DAY_TABLES)
            start, end = schedule.service_window(tables)
            # Counted on the map's lines when the caller names them, as the
            # map build counts, so the two agree on the day.
            day = schedule.busiest_weekday(tables, set(lines) if lines else None, anchor=anchor)
            return {"start": start.isoformat(), "end": end.isoformat(),
                    "busiest_weekday": day.isoformat(), "anchor": anchor.isoformat()}

        return self._job(work)
    # -- the registry (E09c): what feeds there are, adding and forgetting one,
    # and what is in one. The sentences are feeds.py's and inspection.py's.

    def feeds_list(self, params: Any = None) -> dict[str, Any]:
        _no_params("feeds.list", params)
        return {"feeds": [_feed_record(f) for f in feeds.all().values()]}

    def feeds_add(self, params: Any) -> Callable[[], Any]:
        """A feed from a URL or a file the client owns. A long request: the
        download reports its bytes, then the check reports once; a cancel is
        honoured until the moment the feed is kept and leaves nothing."""
        left = _object("feeds.add", params)
        source = left.pop("source", None)
        if not isinstance(source, str) or not source or (
                not source.startswith(("http://", "https://")) and not os.path.isabs(source)):
            raise invalid_params("source must be a URL with its scheme, or an absolute path "
                                 "to a zip the client owns")
        key = _optional(left, "key",
                        lambda v: isinstance(v, str) and bool(KEY_PATTERN.fullmatch(v)),
                        "must be a feed key: lower-case letters, digits and hyphens")
        name = _optional(left, "name", lambda v: isinstance(v, str) and 0 < len(v.strip()) <= 120,
                         "must be text, up to 120 characters")
        overrides = _overrides(left)
        for extra in ("label_pattern", "label_strip"):
            if extra in overrides:
                raise invalid_params(f"feeds.add does not take {extra}")
        # An empty agency is graph.build's word for every operator; a feed
        # added with none simply has none.
        if overrides.get("agency") == feeds.NO_AGENCY:
            raise invalid_params("agency must be an agency_id from the feed, or left out")
        _no_extra("feeds.add", left)

        def work(job: loom.Job, progress: Progress) -> dict[str, Any]:
            feeds_report = _download_report(progress)

            def report(stage: str, done: int, total: int | None) -> None:
                if stage == "download":
                    feeds_report(stage, done, total)
                elif done:
                    progress("check", 1.0, "checked the feed's tables")
            feed = feeds.add(source, key=key, name=name.strip() if name else None,
                             mode=overrides.get("mode", "all"), agency=overrides.get("agency"),
                             progress=report, cancelled=lambda: job.cancelled)
            return _feed_record(feed)

        return self._job(work)

    def feeds_remove(self, params: Any) -> Callable[[], Any]:
        """Forget a feed a person added, with its zips and its layouts. A job,
        so the reader answers other requests meanwhile and reads a cancel. The
        registry's write is the point of no return: a cancel before it leaves
        the feed registered and is answered with the cancelled error; one after
        it is not honoured, the files are removed to the end, and the answer
        carries ``cancel_too_late``."""
        left = _object("feeds.remove", params)
        key = _feed_key(left.pop("key", None))
        _no_extra("feeds.remove", left)
        msg_id = self._request_id

        def work(job: _Job, _progress: Progress) -> dict[str, Any]:
            try:
                feeds.remove(key, cancelled=lambda: job.cancelled, forgotten=job.commit)
            except feeds.FeedError as exc:
                raise EngineError("feed", str(exc)) from exc
            if job.cancelled:
                log.info("cancel for request %s came after the feed was forgotten; "
                         "its files were removed", msg_id)
                return {"ok": True, "cancel_too_late": True}
            return {"ok": True}

        return self._job(work)

    def feeds_inspect(self, params: Any) -> Callable[[], Any]:
        """What is in a feed, as data, from the raw zip: a long request when
        the feed is not cached, and a few seconds for a large one."""
        left = _object("feeds.inspect", params)
        key = _feed_key(left.pop("key", None))
        anchor = _date(left.pop("anchor"), "anchor") if "anchor" in left else dt.date.today()
        _no_extra("feeds.inspect", left)

        def work(_job: loom.Job, _progress: Progress) -> dict[str, Any]:
            return feeds.inspect(key, anchor=anchor).to_dict()

        return self._job(work)

    def render_stage(self, params: Any) -> Callable[[], Any]:
        """One stored stage graph of a layout, drawn, with its counts (E15) and
        its description (issue 54). ``date``, the project's service day, gives
        the description its minutes; left out, they are null and no timetable
        is read. A null date is refused rather than taken for one left out.
        A stage a running build of the layout has finished is drawn from the
        build's scratch, and one it has not is refused as not yet (E27,
        issue 43; ``_drawn_stage``)."""
        left = _object("render.stage", params)
        key = _feed_key(left.pop("key", None))
        layout = _layout(left.pop("layout", None))
        stage = left.pop("stage", None)
        if not isinstance(stage, str) or stage not in pipeline.STAGE_FILES:
            raise invalid_params("stage must be one of " + ", ".join(pipeline.STAGE_FILES))
        width = _positive(left, "width", 1200.0)
        labels = _flag(left, "labels")
        date = None
        if "date" in left:
            value = left.pop("date")
            if value is None:
                raise invalid_params("date must be the service day as YYYY-MM-DD, or left out "
                                     "for a description without minutes")
            date = _date(value)
        _no_extra("render.stage", left)

        def work(_job: loom.Job, _progress: Progress) -> dict[str, Any]:
            svg, counts, description = _drawn_stage(key, layout, stage, width=width,
                                                    labels=labels, date=date)
            width_px, height_px = counts.pop("width"), counts.pop("height")
            return {"layout": layout, "stage": stage, "svg": svg,
                    "width": width_px, "height": height_px, "counts": counts,
                    "description": description}

        return self._job(work)

    def style_presets(self, params: Any = None) -> dict[str, Any]:
        """The named looks ``map.build``'s ``style`` takes in place of its
        fields: each a name and the eight numbers it resolves to, as an
        object a client could send field by field, in the table's order."""
        _no_params("style.presets", params)
        return {"presets": [{"name": name, "style": preset_style(name)} for name in PRESETS]}

    # -- export: the two halves the desktop app cannot do itself. It captures
    # for itself (its ADR-024); there is no export.capture here.

    def export_presets(self, params: Any = None) -> dict[str, Any]:
        _no_params("export.presets", params)
        return {"presets": export.preset_table()}

    def export_storyboards(self, params: Any = None) -> dict[str, Any]:
        _no_params("export.storyboards", params)
        return {"storyboards": export.storyboard_table()}

    def export_plan(self, params: Any) -> dict[str, Any]:
        """Pure and instant: the plan's own refusals (a vector preset, a
        geographic view on a feed without the geometry) are export errors."""
        left = _object("export.plan", params)
        key = _feed_key(left.pop("key", None))
        preset = _preset(left.pop("preset", None))
        page = _optional(left, "page",
                         lambda v: isinstance(v, str) and bool(URL_PATTERN.fullmatch(v)),
                         "must be the page's address, with its scheme")
        date = _date(left.pop("date")).isoformat() if "date" in left else None
        options = _export_options(left.pop("options", None))
        _no_extra("export.plan", left)
        # Refused here rather than by export.plan, so it is the caller's
        # mistake (params) and not the export's; a still ignores a list.
        if (export.PRESETS[preset].kind == "video"
                and not isinstance(options.get("storyboard", ""), str)
                and (options.get("view") or options.get("at"))):
            raise invalid_params(export.BESIDE_A_LIST)
        try:
            job = export.plan(key, preset, page=page, date=date, **options)
        except (KeyError, ValueError) as exc:
            raise classify(exc) from exc
        return {**job.to_dict(), "filename": job.filename}

    def export_encode(self, params: Any) -> Callable[[], Any]:
        """The frames the client captured, or its still, to the file. Long:
        ffmpeg's own frame count arrives as job/progress, and a cancel ends
        ffmpeg and removes the partial file."""
        left = _object("export.encode", params)
        job = _capture_job(left.pop("plan", None))
        source = _absolute(left, "source")
        dest = _absolute(left, "dest")
        provenance = _provenance(left.pop("provenance", None))
        _no_extra("export.encode", left)
        if job.mode == "video" and not source.is_dir():
            raise EngineError("io", "the frames directory is not there")
        if job.mode == "still" and not source.is_file():
            raise EngineError("io", "the captured still is not there")

        def work(_job: loom.Job, progress: Progress) -> dict[str, Any]:
            written = export.encode(job, source, dest, provenance=provenance,
                                    progress=progress)
            sidecar = json.loads(export.sidecar_path(dest).read_text(encoding="utf-8"))
            return {"files": [{"path": str(f), "bytes": f.stat().st_size} for f in written],
                    "sidecar": sidecar}

        return self._job(work)


def _download_report(progress: Progress) -> feeds.DownloadProgress:
    """A download's bytes as job/progress, stage ``download``: the fraction
    of the bytes when the server said how many, else 0, and the sentence
    ``feeds.add`` has always sent. One function for an add and for a
    preset's first fetch inside any long request (E36)."""
    def report(stage: str, done: int, total: int | None) -> None:
        if stage != "download":
            return
        fraction = min(done / total, 1.0) if total else 0.0
        message = (f"downloaded {done:,} of {total:,} bytes" if total
                   else f"downloaded {done:,} bytes")
        progress("download", fraction, message)
    return report


def _stage_summary(graph: LineGraph) -> dict[str, Any]:
    return render_summary(graph)


def _drawn_stage(key: str, layout: str, stage: str, *, width: float, labels: bool,
                 date: dt.date | None) -> tuple[str, dict, dict]:
    """``render.stage``'s drawing (E15, E27): from the scratch of a build of
    the layout running in this process, once the build has finished what the
    answer needs -- the stage, and the octi stage too when ``date`` asks for
    the minutes, which are read from it -- and drawn as a stored stage is;
    otherwise from the store, as before. A layout that is building and not
    stored is refused as not yet, with the layout, the stage it waits on and
    ``building`` in the error's data, so a client tells it from one that is
    not stored without reading the sentence and asks again on its next
    progress report. Nothing holds the lock while it draws."""
    needed = [stage] if date is None or stage == "octi" else [stage, "octi"]
    try:
        building = pipeline.in_flight(key, layout, needed)
    except pipeline.NotYet as exc:
        # A forced re-layout's stored set answers, as it did before the build
        # began, until the build has finished what the answer needs.
        if pipeline.read_layout(key, layout) is not None:
            building = None
        else:
            hint = str(exc)
            if exc.stage != stage:
                hint += (f"; the minutes of a date are read from the {exc.stage} stage, so "
                         f"leave date out to draw {stage} now")
            raise EngineError("layout", hint, layout=layout, stage=exc.stage,
                              building=True) from None
    if building is None:
        return render_stage(key, stage, layout=layout, width=width, labels=labels, date=date)
    found, files = building
    graphs = {name: LineGraph.from_geojson(json.loads(data)) for name, data in files.items()}
    minutes = (pipeline.line_minutes(found, date, graph=graphs["octi"])
               if date is not None else None)
    return draw_stage(graphs[stage], stage, width=width, labels=labels, minutes=minutes)


def _stations(graph: LineGraph) -> list[dict[str, str]]:
    """Every station the map draws, as ``map.build`` answers them: the node's
    id, which is what the page's ``setTrip`` takes, and the name the map
    writes (empty for a station the feed gives none). Sorted by name, as code
    points, then by id, which tells apart the stations that share a name."""
    return sorted(({"id": node.id, "name": station_name(node)} for node in graph.stations),
                  key=lambda station: (station["name"], station["id"]))


def _feed_record(feed: feeds.Feed) -> dict[str, Any]:
    """A registry entry as the protocol shows it: the record, and whether its
    zip is on disk, which is what "downloaded" means to a person."""
    record = feed.to_dict()
    record["cached"] = feed.zip_path.is_file()
    return record


# ------------------------------------------------------------------- transport

# Codes the library logs with a full traceback at ERROR although they are the
# protocol working as designed: bad parameters, an unknown method, a feed
# error with its hint, a cancellation. Anything else it logs still shows.
EXPECTED_CODES = frozenset({-32601, -32602, ENGINE_ERROR, JsonRpcRequestCancelled.CODE})


class _QuietExpectedErrors(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        return not (isinstance(exc, JsonRpcException) and exc.code in EXPECTED_CODES)


def serve(rfile: IO[bytes], wfile: IO[bytes]) -> None:
    """Answer requests read from ``rfile`` on ``wfile`` until it closes or
    ``engine.shutdown`` is answered. Nothing the server started outlives it."""
    logging.getLogger("pylsp_jsonrpc.endpoint").addFilter(_QuietExpectedErrors())
    writer = JsonRpcStreamWriter(wfile)
    endpoint = EngineEndpoint(writer.write)
    reader = JsonRpcStreamReader(rfile)

    def consume(message: dict[str, Any]) -> None:
        endpoint.consume(message)
        if endpoint.stopping:
            reader.close()

    try:
        reader.listen(consume)
    finally:
        endpoint.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m schematic.serve",
        description="Serve the engine over JSON-RPC 2.0 on stdin and stdout.")
    parser.add_argument("--schema", action="store_true",
                        help="print the protocol's JSON Schema and exit")
    args = parser.parse_args(argv)
    if args.schema:
        # Bytes, not text: a text-mode stdout on Windows turns the newline
        # into CRLF, and the desktop app pins this output by its hash.
        sys.stdout.buffer.write((json.dumps(schema(), indent=2) + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()
        return 0

    level = os.environ.get("SCHEMATIC_LOG", "info").upper()
    if level not in logging.getLevelNamesMapping():
        level = "INFO"
    logging.basicConfig(stream=sys.stderr, level=level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    rfile, wfile = sys.stdin.buffer, sys.stdout.buffer
    # stdout is the protocol stream. Anything else the engine prints -- the
    # site's notes, a stray debug line -- goes to stderr with the logs rather
    # than into the middle of a frame.
    sys.stdout = sys.stderr
    log.info("engine %s, protocol %d, home %s", __version__, PROTOCOL, config.home())
    serve(rfile, wfile)
    return 0


if __name__ == "__main__":
    sys.exit(main())
