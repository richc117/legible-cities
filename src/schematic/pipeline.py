"""End-to-end pipeline: GTFS feed in, schematic map and animation out.

The LOOM stages are the slow half -- ``gtfs2graph`` takes about ten seconds,
the rest is instant -- and a layout, once made, is what every map and export
of a network reads. So a layout is stored under the engine's home (see
``config``) at ``data/graphs/<feed>/<id>/``, named by the hash of everything
that went into it: the feed's bytes, the mode, the agency, the label options,
the LOOM build and the stage arguments. The same inputs name the same
directory before anything runs; a change to any of them names a new one and
leaves the old untouched. A layout is written whole into a scratch directory
and moved into place, never stage by stage into the directory a reader
might be reading, so a cancel or a failure leaves nothing that looks
finished. While it is written, the stages it has finished can be read from
its scratch and nowhere else (``in_flight``). ``lay_out`` makes one,
``stored`` finds one, ``run`` draws from one.

    from schematic import pipeline
    result = pipeline.run("la-metro-rail")
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping

from . import __version__, animate, config, feeds, loom
from . import diagnostics as diagnostics_module
from .crs import to_mercator
from .linegraph import LineGraph
from .render import RenderResult, Style, check_color, render
from .schedule import (StopMatch, Trip, busiest_weekday, match_stops, service_day_text,
                       trips_on)

# The LOOM stages, in order, with the arguments we run them with. Kept as data
# so a notebook can print the pipeline or re-run one stage with a tweak. The
# tuples are empty on purpose: every tool runs at its own defaults, and this
# list is what an untuned layout is addressed by, so an argument written here
# moves the id of every layout stored. A tuning's flags are appended to a
# copy of it by ``stages_for`` and nowhere else.
STAGES: list[tuple[str, tuple[str, ...]]] = [
    ("topo", ()),
    ("loom", ()),
    ("octi", ()),
]

# Called as each step finishes: (stage, fraction of the whole done, a line
# about what it produced). The JSON-RPC server turns these into notifications;
# a notebook can print them; nothing here waits on the callback.
Progress = Callable[[str, float, str], None]


# ------------------------------------------------------------------- tuning
#
# A layout can be tuned with LOOM's own flags, by name and with no slider
# mapped over them (issue 37). This table is the one place the protocol's
# names meet the tools' flags, LOOM's defaults and the ranges a layout is
# worth asking for; ``serve._tuning`` judges a request against it and
# ``stages_for`` turns what passed into the stage tuples the layout is
# addressed by.

@dataclass(frozen=True)
class Tunable:
    """One number LOOM takes: the stage it belongs to, its flag, the value it
    has when the flag is not given, the closed range worth asking for, and
    what the number counts, for the sentence that refuses it."""

    tool: str
    flag: str
    default: float
    low: float
    high: float
    unit: str
    suffix: str = ""


GRID_TOOL = "octi"
GRID_FLAG = "-b"
GRIDS = ("octilinear", "ortholinear", "orthoradial", "hexalinear")
GRID_DEFAULT = "octilinear"

# topo's station-merge radius and octi's grid pitch. ``-g`` takes a
# percentage of the distance between adjacent stations when its value ends
# in a percent sign, so the pitch is written with one.
MERGE_DISTANCE = Tunable("topo", "-d", 50, 5, 500, "in metres")
GRID_SIZE = Tunable("octi", "-g", 100, 25, 400,
                    "as a percentage of the distance between adjacent stations", "%")

# octi's costs, in the order their flags are written: one per angle a route can
# make at a node (``--pen-N``, N in degrees) and one for a diagonal edge. They
# are costs in LOOM's own scale, so numbers without a unit.
PENALTIES: dict[str, Tunable] = {
    name: Tunable("octi", flag, default, 0, 10, "as a cost without a unit")
    for name, flag, default in (("deg45", "--pen-45", 2), ("deg90", "--pen-90", 1.5),
                                ("deg135", "--pen-135", 1), ("deg180", "--pen-180", 0),
                                ("diagonal", "--diag-pen", 0.5))}


def _written(value: float) -> str:
    """A number as LOOM's own help prints one: ``50`` for 50.0 and ``1.5`` for
    1.5, never ``50.0``. A flag's text is part of the layout's id, so equal
    numbers must be written identically, whether the client sent an integer
    or a float."""
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def _flag(tunable: Tunable, value: float | None) -> tuple[str, ...]:
    """The flag for a value, or none when it is left out or is LOOM's own
    default: a default writes nothing, so the layout stays the one every
    untuned build already names."""
    if value is None or value == tunable.default:
        return ()
    return (tunable.flag, _written(value) + tunable.suffix)


def stages_for(tuning: Mapping[str, Any] | None = None) -> list[tuple[str, tuple[str, ...]]]:
    """``STAGES`` with the flags of a tuning appended: topo's on ``topo``, octi's
    on ``octi``, ``loom``'s as it was.

    The tuning is one ``serve._tuning`` has judged: the fields of
    ``GraphBuildParams.tuning``, each optional, ``penalties`` an object of its
    own. Pure, and the same tuning always gives the same tuples: the flags are
    written in the table's order whatever order the client named them in; a
    field equal to LOOM's default writes none, so ``None``, ``{}`` and a tuning
    of only defaults give exactly ``STAGES``; and numbers are written the way
    LOOM prints them (``_written``). The result is the ``stages`` that
    ``lay_out`` hashes into the layout's id and records in its meta."""
    tuning = tuning or {}
    penalties = tuning.get("penalties") or {}
    flags: dict[str, list[str]] = {tool: [] for tool, _args in STAGES}
    flags[MERGE_DISTANCE.tool] += _flag(MERGE_DISTANCE, tuning.get("merge_distance"))
    grid = tuning.get("grid")
    if grid not in (None, GRID_DEFAULT):
        flags[GRID_TOOL] += [GRID_FLAG, grid]
    flags[GRID_SIZE.tool] += _flag(GRID_SIZE, tuning.get("grid_size"))
    for name, tunable in PENALTIES.items():
        flags[tunable.tool] += _flag(tunable, penalties.get(name))
    return [(tool, (*args, *flags[tool])) for tool, args in STAGES]


def graph_dir(key: str) -> Path:
    """The layouts of one feed, created if it is missing."""
    d = config.graphs_dir() / key
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------------ layouts

META_FILE = ".meta.json"

# The stage files a layout holds, in pipeline order; the number is the
# stage's place, as it always was.
STAGE_FILES: dict[str, str] = {"gtfs2graph": "00_gtfs2graph.json",
                               **{tool: f"{i:02d}_{tool}.json"
                                  for i, (tool, _args) in enumerate(STAGES, start=1)}}


LAYOUT_ID_PATTERN = r"^[0-9a-f]{64}$"
_ID = re.compile(LAYOUT_ID_PATTERN)


class LayoutMissing(ValueError):
    """A layout was named that is not stored: nothing was run, nothing can be
    drawn. Its own kind on the protocol (``layout``), since no tool failed."""


@dataclass(frozen=True)
class Layout:
    """One stored layout: its id, where it is, and what went into it."""

    key: str
    id: str
    dir: Path
    meta: dict[str, Any]

    @property
    def paths(self) -> dict[str, Path]:
        return {stage: self.dir / name for stage, name in STAGE_FILES.items()}

    @property
    def feed(self) -> feeds.Feed:
        """The feed as this layout was built from it: the registry entry with
        every override the meta records, a recorded null included -- the
        registry may have gained an agency since, and this layout never
        applied one."""
        recorded = {name: self.meta[name] for name in feeds.OVERRIDES if name in self.meta}
        return replace(feeds.get(self.key), **recorded)


_digests: dict[tuple[str, int, int], str] = {}


def feed_digest(path: Path) -> str:
    """The sha256 of a feed zip, remembered by size and modification time so a
    three-hundred-megabyte feed is read once per process, not per build."""
    st = path.stat()
    marker = (str(path), st.st_ino, st.st_size, st.st_mtime_ns)
    digest = _digests.get(marker)
    if digest is None:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        digest = _digests[marker] = h.hexdigest()
    return digest


def layout_inputs(feed: feeds.Feed, *, feed_sha256: str, loom_commit: str | None,
                  stages: list[tuple[str, tuple[str, ...]]] | None = None) -> dict[str, Any]:
    """Everything a layout depends on, as data. Its hash is the layout's id,
    so it holds nothing that is not an input: no version, no date."""
    return {
        "feed": feed.key,
        "feed_sha256": feed_sha256,
        "mode": feed.mode,
        "agency": feed.agency,
        "label_pattern": feed.label_pattern,
        "label_strip": feed.label_strip,
        "loom": loom_commit,
        # gtfs2graph's own arguments first, so a flag added to it later is
        # part of the id without anyone having to remember.
        "stages": [["gtfs2graph", ["-m", feed.mode]],
                   *([tool, list(args)] for tool, args in (stages or STAGES))],
    }


def layout_id(inputs: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(inputs, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def layout_dir(key: str, layout: str) -> Path:
    return config.graphs_dir() / key / layout


def read_layout(key: str, layout: str) -> Layout | None:
    """The stored layout with this id, or None when it is not there whole."""
    d = layout_dir(key, layout)
    meta_path = d / META_FILE
    if not meta_path.is_file() or not all((d / name).is_file() for name in STAGE_FILES.values()):
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return Layout(key=key, id=layout, dir=d, meta=meta)


def stored_layouts(key: str) -> list[Layout]:
    """Every whole layout stored for a feed, newest first."""
    folder = config.graphs_dir() / key
    if not folder.is_dir():
        return []
    found = [read_layout(key, child.name) for child in folder.iterdir()
             if child.is_dir() and _ID.match(child.name)]
    return sorted((l for l in found if l is not None),
                  key=lambda l: str(l.meta.get("made", "")), reverse=True)


def _address(feed: feeds.Feed, source: Path,
             stages: list[tuple[str, tuple[str, ...]]] | None = None) -> tuple[dict[str, Any], str]:
    inputs = layout_inputs(feed, feed_sha256=feed_digest(source), loom_commit=loom.commit(),
                           stages=stages)
    return inputs, layout_id(inputs)


def stored(key: str, **overrides: Any) -> Layout | None:
    """The layout the feed as it is on disk, with these options and this LOOM,
    names -- if it is stored. Never builds and never downloads; None when the
    feed is not on disk or the layout is not there. A set from before layouts
    had names is migrated on the way (see ``migrate``)."""
    feed = feeds.resolved(key, **overrides)
    source = feeds.get(key).zip_path
    if not source.is_file():
        return None
    inputs, layout = _address(feed, source)
    return read_layout(key, layout) or _migrated(feed, layout, inputs)


def _migrated(feed: feeds.Feed, layout: str, inputs: dict[str, Any]) -> Layout | None:
    """A flat set was built from the registry entry and nothing else, so only
    the registry's own layout may adopt it."""
    if feeds.variant(feed) is not None:
        return None
    return migrate(feed.key, layout, inputs)


def stage_path(key: str, stage: str, **overrides: Any) -> Path | None:
    """One stage file of the stored layout, or None: what a test or the site
    reads when it wants a graph without building one."""
    found = stored(key, **overrides)
    return None if found is None else found.paths[stage]


_lock = threading.Lock()
_building: dict[tuple[str, str], threading.Event] = {}


@dataclass
class _Scratch:
    """Where a build in flight is writing, and which of its stage files are
    whole there (E27, engine issue 43). A stage joins ``whole`` once its file
    is written and before its report goes out, so a client that asks for it on
    the report finds it, and never before the write, so nobody reads half a
    file. The inputs are the meta the build will write, which a description's
    minutes read the feed from."""

    dir: Path
    inputs: dict[str, Any]
    whole: set[str] = field(default_factory=set)


# Beside ``_building`` and under the same lock: a build registers its scratch
# when it makes it, and the registration goes before ``_building`` forgets the
# build -- once that has gone another build of the layout may start and
# register its own -- and, on a failure, before the scratch is removed.
_scratches: dict[tuple[str, str], _Scratch] = {}


class NotYet(LayoutMissing):
    """A stage of a layout that a build in this process is laying out and has
    not finished yet. A client asks again on its next progress report; still a
    ``LayoutMissing``, so anything that does not tell them apart refuses it as
    the same kind, ``layout``."""

    def __init__(self, key: str, layout: str, stage: str) -> None:
        super().__init__(f"{key!r} is still being laid out under layout {layout[:8]}, and the "
                         f"build has not reached its {stage} stage yet; ask again when "
                         f"job/progress reports it")
        self.layout = layout
        self.stage = stage


def in_flight(key: str, layout: str, stages: list[str]) -> tuple[Layout, dict[str, bytes]] | None:
    """The files of ``stages`` of a layout this process is laying out, read
    whole from its build's scratch, with the layout as the build will store it:
    what ``render.stage`` draws before the run ends (E27, engine issue 43).

    None when no build of the layout is running here, or when its scratch has
    gone (the swap has moved it into place since): the store answers then, as
    it always has. A stage the build has not finished raises ``NotYet``, the
    first such of ``stages`` named.

    The files are read under the lock and parsed by the caller outside it: the
    swap takes the lock too, so it never moves a directory with a file of it
    open, which Windows refuses. The ``Layout``'s directory is the scratch and
    its meta the build's inputs: what a description's minutes need, the files
    being the ones returned beside it, never read from that directory.
    """
    with _lock:
        scratch = _scratches.get((key, layout))
        if scratch is None:
            return None
        for stage in stages:
            if stage not in scratch.whole:
                raise NotYet(key, layout, stage)
        try:
            files = {stage: (scratch.dir / STAGE_FILES[stage]).read_bytes() for stage in stages}
        except FileNotFoundError:
            return None
    return Layout(key=key, id=layout, dir=scratch.dir, meta=dict(scratch.inputs)), files


def _whole(key: str, layout: str, stage: str) -> None:
    """A stage's file is written: a reader may have it from now on."""
    with _lock:
        _scratches[(key, layout)].whole.add(stage)


def _forget(key: str, layout: str) -> None:
    """The build's scratch is no longer to be read from."""
    with _lock:
        _scratches.pop((key, layout), None)


def lay_out(key: str, *, force: bool = False,
            stages: list[tuple[str, tuple[str, ...]]] | None = None,
            progress: Progress | None = None, **overrides: Any) -> Layout:
    """The layout for a feed and its options: found, or made.

    Made: gtfs2graph, then the stages, into a scratch directory beside the
    target, moved into place once ``octi`` has returned and the meta is
    written. ``force`` makes a new one under the same id; the one already
    there stays readable until the new set is whole, and is then replaced.
    Progress is reported per stage either way, so a caller sees the same
    four reports whether anything ran.
    """
    stages = stages or STAGES
    feed = feeds.resolved(key, **overrides)
    source = feeds.fetch(key)
    inputs, layout = _address(feed, source, stages)
    # One build of one layout at a time in this process: a second caller
    # for the same id -- two projects on one feed -- waits for the first
    # and reads what it stored rather than building beside it.
    waited = 0.0
    while True:
        with _lock:
            in_flight = _building.get((key, layout))
            if in_flight is None:
                existing = None
                if not force:
                    existing = read_layout(key, layout) or _migrated(feed, layout, inputs)
                if existing is None:
                    _building[(key, layout)] = threading.Event()
                break
        began = time.monotonic()
        in_flight.wait()
        waited += time.monotonic() - began
    # Reported outside the lock: a report parses four files and writes to the
    # client, and nothing else in the process should wait on either.
    if existing is not None:
        for stage, path in existing.paths.items():
            _report(progress, stage, _fraction(stage, stages), path, existing.id)
        _from_store(existing, waited)
        return existing
    try:
        return _build(feed, layout, inputs, stages, progress)
    finally:
        with _lock:
            _scratches.pop((key, layout), None)
            _building.pop((key, layout)).set()


def _fraction(stage: str, stages: list[tuple[str, tuple[str, ...]]]) -> float:
    names = ["gtfs2graph", *(tool for tool, _args in stages)]
    return (names.index(stage) + 1) / len(names)


def _build(feed: feeds.Feed, layout: str, inputs: dict[str, Any],
           stages: list[tuple[str, tuple[str, ...]]], progress: Progress | None) -> Layout:
    key = feed.key
    folder = graph_dir(key)
    with _lock:
        _sweep(folder)
    # A scratch of this build's own, named with the process so a sweep from
    # another can tell a live build from what a crash left.
    scratch = Path(tempfile.mkdtemp(prefix=f"{layout}.building-{os.getpid()}-", dir=folder))
    # Readable as each stage finishes (E27): ``in_flight`` reads it, and
    # ``lay_out`` forgets it before it forgets the build.
    with _lock:
        _scratches[(key, layout)] = _Scratch(dir=scratch, inputs=inputs)
    try:
        normalized = feeds.normalize(feed)
        # From here: normalizing is the feed's, not gtfs2graph's. The native
        # backend's first unpack of the zip is inside the call and counted.
        since = time.monotonic()
        graph = loom.gtfs2graph(normalized, "-m", feed.mode)
        out = scratch / STAGE_FILES["gtfs2graph"]
        out.write_text(json.dumps(graph), encoding="utf-8", newline="\n")
        _whole(key, layout, "gtfs2graph")
        _stage_done(progress, "gtfs2graph", _fraction("gtfs2graph", stages), out, since, layout)
        payload = out.read_bytes()
        for tool, args in stages:
            since = time.monotonic()
            payload = json.dumps(loom.run(tool, payload, *args)).encode()
            out = scratch / STAGE_FILES[tool]
            out.write_bytes(payload)
            _whole(key, layout, tool)
            _stage_done(progress, tool, _fraction(tool, stages), out, since, layout)
        meta = {**inputs, "engine": __version__,
                "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "migrated": False}
        (scratch / META_FILE).write_text(json.dumps(meta, indent=2) + "\n",
                                         encoding="utf-8", newline="\n")
    except BaseException:
        # A cancel, a failure, an interrupt: the scratch goes, the stored
        # layout, if there was one, was never touched. Forgotten first, under
        # the lock a reader holds while it reads: Windows will not remove a
        # file that is open.
        _forget(key, layout)
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    try:
        with _lock:
            _swap(scratch, layout_dir(key, layout))
    except BaseException:
        _forget(key, layout)
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    found = read_layout(key, layout)
    assert found is not None
    return found


def _swap(scratch: Path, target: Path) -> None:
    """The finished set into place. What was there goes aside first and is
    removed after, so the target is never a directory with some of each."""
    aside: Path | None = None
    if target.exists():
        aside = target.with_name(f"{target.name}.old-{os.getpid()}")
        shutil.rmtree(aside, ignore_errors=True)
        os.replace(target, aside)
    os.replace(scratch, target)
    if aside is not None:
        shutil.rmtree(aside, ignore_errors=True)


_LEFTOVER = re.compile(r"^(?P<layout>[0-9a-f]{64})\.(?P<what>building|old)-(?P<pid>\d+)")


def _alive(pid: int) -> bool:
    """Whether the process that made a scratch is still around. Where that
    cannot be asked (Windows), a foreign process is presumed alive: a
    leftover kept is cheaper than a build destroyed."""
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep(folder: Path) -> None:
    """What a crash leaves, and only that: a scratch whose process is gone,
    or a set put aside by one. A live build's scratch, and a set another
    process has put aside mid-swap, are theirs. A set put aside whose target
    is missing is the stored layout a crash interrupted between the two
    renames, and comes back; the rest goes."""
    for child in folder.iterdir():
        m = _LEFTOVER.match(child.name)
        if m is None or not child.is_dir() or _alive(int(m["pid"])):
            continue
        if m["what"] == "building":
            shutil.rmtree(child, ignore_errors=True)
            continue
        target = folder / m["layout"]
        if target.exists():
            shutil.rmtree(child, ignore_errors=True)
        else:
            os.replace(child, target)


def migrate(key: str, layout: str, inputs: dict[str, Any]) -> Layout | None:
    """A set from before layouts had names -- four files flat under the
    feed's folder -- becomes the layout the feed's current inputs name, once.
    Moved, not copied, so the site's cache survives the change in place. The
    inputs recorded are the feed and options as they are now; the set was
    made before they were recorded, which the meta says with ``migrated``."""
    folder = config.graphs_dir() / key
    flat = [folder / name for name in STAGE_FILES.values()]
    if not all(path.is_file() for path in flat):
        return None
    target = layout_dir(key, layout)
    with _lock:
        if target.exists():
            return read_layout(key, layout)
        scratch = Path(tempfile.mkdtemp(prefix=f"{layout}.building-{os.getpid()}-", dir=folder))
        for path in flat:
            shutil.move(str(path), str(scratch / path.name))
        meta = {**inputs, "engine": __version__,
                "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "migrated": True}
        (scratch / META_FILE).write_text(json.dumps(meta, indent=2) + "\n",
                                         encoding="utf-8", newline="\n")
        os.replace(scratch, target)
    return read_layout(key, layout)


def line_graph(key: str, *, force: bool = False) -> Path:
    """Stage 0 of the stored or newly made layout: GTFS -> LOOM line graph."""
    return lay_out(key, force=force).paths["gtfs2graph"]


def schematize(key: str, *, force: bool = False,
               stages: list[tuple[str, tuple[str, ...]]] | None = None,
               progress: Progress | None = None, **overrides: Any) -> dict[str, Path]:
    """The stage files of the layout for a feed, made if it is not stored.
    Returns stage name -> path; ``lay_out`` returns the layout itself."""
    return lay_out(key, force=force, stages=stages, progress=progress, **overrides).paths


def _report(progress: Progress | None, stage: str, fraction: float, path: Path,
            layout: str) -> None:
    if progress is not None:
        _tell(progress, layout, stage, fraction, LineGraph.from_geojson(path).summary())


# The layout whose stage is being reported, while the report's callback runs
# on this thread (E27, engine issue 43). ``Progress`` keeps its three
# arguments, so every caller's callback hears what it always heard; the
# server's asks ``reporting()`` and names the layout in job/progress.
_reporting = threading.local()


def reporting() -> str | None:
    """The id of the layout a stage report is of, asked from inside that
    report's callback; None in any other report, a download's included, and
    outside one."""
    return getattr(_reporting, "layout", None)


def _tell(progress: Progress, layout: str, stage: str, fraction: float, message: str) -> None:
    previous = reporting()
    _reporting.layout = layout
    try:
        progress(stage, fraction, message)
    finally:
        _reporting.layout = previous


def _log(line: str) -> None:
    """A line of the engine's own in the running request's log (E37).

    A request's log was the LOOM tools' stderr and nothing else, and the
    native tools write nothing when they succeed, so a run that went well
    sent no ``job/log`` at all. These lines say what each stage did. Outside
    a request there is no job and nothing is written: the command line and
    the site have ``Result.summary()``.

    Never a path: the client redacts a web address's secrets in a log line
    but not a file's location, so a file is named by its name alone.
    """
    job = loom.current_job()
    if job is not None:
        job.log(line)


def _took(since: float) -> str:
    """How long since ``since`` (``time.monotonic()``), as a line says it."""
    return f"{time.monotonic() - since:.1f} s"


def _stage_done(progress: Progress | None, stage: str, fraction: float, path: Path,
                since: float, layout: str) -> None:
    """A LOOM stage of ``layout`` has written ``path``: report it, and log it
    with its time, taken before the file is read back, which is not the
    stage's."""
    took = _took(since)
    if progress is None and loom.current_job() is None:
        return
    summary = LineGraph.from_geojson(path).summary()
    if progress is not None:
        _tell(progress, layout, stage, fraction, summary)
    _log(f"{stage}: {summary} ({took})")


def _from_store(layout: Layout, waited: float = 0.0) -> None:
    """Say that a layout was read, not made: its stages did not run in this
    request. A request that waited for another's build of it says how long.

    It does not say "nothing was laid out" (it did at 0.9.0): a client that
    lays out and then draws sends two requests and shows one log, where the
    draw's line followed the layout's four and read as a denial of them."""
    line = f"layout {layout.id[:8]}: read from the store"
    if waited > 0:
        line += f" (waited {waited:.1f} s for another build of it)"
    _log(line)


def require_edges(feed_or_key: feeds.Feed | str, graph: LineGraph) -> None:
    """Refuse an empty graph with the sentence about modes. Raised here rather
    than returned so the error is the pipeline's, wherever it is checked. A
    ``Feed`` names the mode the layout was actually built with."""
    feed = feed_or_key if isinstance(feed_or_key, feeds.Feed) else feeds.get(feed_or_key)
    if not graph.edges:
        raise ValueError(
            f"{feed.key}: the line graph is empty -- gtfs2graph -m {feed.mode!r} "
            f"matched no routes. Check the feed's route_type values; agencies "
            f"disagree about which of tram/subway/rail their network is.")


@dataclass
class Result:
    key: str
    date: dt.date
    layout: str               # the stored layout the map was drawn from
    graph: LineGraph          # projected, ready to draw
    render: RenderResult
    trips: list[Trip]
    match: StopMatch
    animation: animate.Animation
    paths: dict[str, Path]

    def peak_concurrent(self) -> int:
        """The most trains under way at once, sampled on the hour."""
        return max(len(self.animation.trips) and
                   sum(1 for t in self.animation.trips
                       if t["k"][0][0] <= h * 3600 <= t["k"][-1][0])
                   for h in range(24))

    def diagnostics(self) -> diagnostics_module.Diagnostics:
        """The build's numbers, as data: one rendering for the terminal, the
        site and the app (``diagnostics.py``)."""
        return diagnostics_module.Diagnostics.of(self)

    def summary(self) -> str:
        return self.diagnostics().summary()


def run(key: str, *, layout: str | None = None, date: dt.date | None = None,
        anchor: dt.date | None = None,
        width: float = 1800.0, style: Style | None = None,
        colors: dict[str, str] | None = None, default_color: str | None = None,
        line_order: list[str] | None = None, lines: dict[str, dict] | None = None,
        force: bool = False, out_dir: Path | None = None, back: str = "index.html",
        icons: str | None = None, social: str = "",
        progress: Progress | None = None) -> Result:
    """Everything: the layout, then draw, schedule, animate, write.

    ``layout`` names a stored layout to draw from, and then nothing is laid
    out: a missing one is refused. Without it the feed's registry entry is
    laid out if it is not stored yet (``force`` again), which is what the
    command line and the site want. ``date`` is the service day; without
    one the busiest weekday is chosen scanning from ``anchor``, which is
    today unless the caller says otherwise. ``colors`` overrides a line's
    colour by label and ``default_color`` is the colour of a line the feed
    leaves uncoloured, both written ``#rrggbb`` and both reaching the map
    and the page alike (``render.line_colors``); ``line_order`` is the
    stacking on shared track, the later over the earlier; the lines it
    leaves out follow the ones it names rather than going undrawn. ``lines``
    is what a client chose per line, by label, as ``serve._lines`` checked it:
    a ``name`` the page writes where it writes the label, and ``hidden``, which
    takes the line off the graph before anything reads it (app ADR-053), so
    it has no track, trips, chip, row or band and a station only it served is
    not drawn; nothing stored changes, and a label the layout does not carry
    is ignored. ``back`` is the href the animation page's
    back-link points at. The default is the sibling gallery in ``out/``;
    the site passes its own atlas URL, because a relative "index.html"
    resolves to /maps/index.html there.
    """
    steps = ("gtfs2graph", "topo", "loom", "octi", "schedule", "render", "animate", "write")

    def tick(stage: str, message: str = "") -> None:
        if progress is not None:
            progress(stage, (steps.index(stage) + 1) / len(steps), message)

    # The draw's own stages, logged as the layout's are: what each did and
    # how long it took, from the end of the one before (E37). The clock
    # starts once the layout is in hand, below.

    def done(stage: str, message: str, line: str | None = None) -> None:
        nonlocal since
        tick(stage, message)
        _log(f"{stage}: {line if line is not None else message} ({_took(since)})")
        since = time.monotonic()

    if layout is not None:
        found = read_layout(key, layout)
        if found is None:
            raise LayoutMissing(f"{key} has no stored layout {layout[:8]}; lay the feed out "
                                f"first (graph.build)")
        if progress is not None:
            for stage, path in found.paths.items():
                _report(lambda st, _f, m: tick(st, m), stage, 0.0, path, found.id)
        _from_store(found)
    else:
        found = lay_out(key, force=force,
                        progress=(lambda stage, _f, m: tick(stage, m)) if progress else None)
    paths = found.paths
    since = time.monotonic()

    # Match stops against the unprojected graph -- station_id is what matters
    # there, and reprojecting is only needed for geometry.
    graph_ll = LineGraph.from_geojson(paths["octi"])
    require_edges(found.feed, graph_ll)
    # A hidden line comes off the graph here, once, so the stop matching, the
    # schedule, the drawing, the thumbnails and the page see only the lines
    # that remain (app ADR-053). The stored layout is read, never changed.
    every = set(graph_ll.labels)
    hidden = {label for label, chosen in (lines or {}).items()
              if chosen.get("hidden") and label in every}
    if hidden and hidden == every:
        raise ValueError(f"{key}: every line on this map is hidden; show at least one line "
                         f"to draw it")
    graph_all = graph_ll
    if hidden:
        graph_ll = graph_ll.without(hidden)
    graph = graph_ll.reproject(to_mercator)

    # The day is read as render.stage's description reads it (schedule_for),
    # over every line the layout carries: the minutes cache it fills describes
    # the whole layout, and the service day is counted over every line, so
    # hiding one never moves it (app ADR-053). The drawn map then keeps the
    # drawn lines' trips alone, and the match it reports is cut to the
    # stations that remain.
    day = schedule_for(found, date, anchor=anchor, graph=graph_all)
    _remember_minutes(found.id, day)
    date, match, trips = day.date, day.match, day.trips
    labels = set(graph_ll.labels)
    if hidden:
        kept = set(graph_ll.nodes)
        match = replace(match, stop_to_node={stop: node for stop, node in match.stop_to_node.items()
                                             if node in kept})
        trips = [trip for trip in trips if trip.route_label in labels]
    done("schedule", f"{len(trips)} trips on {service_day_text(date)}; {match.report()}")

    name = feeds.get(key).name
    # Themed by default: the CSS variables carry literal fallbacks, so a
    # standalone SVG is unchanged, while an embedding page can theme the
    # furniture without resorting to an invert filter over the line colours.
    style = style or Style(themed=True)
    if default_color is not None:
        style = replace(style, default_line_color=check_color(default_color, "default_color"))
    r = render(graph, width=width, style=style, title=name, line_order=line_order,
               colors=colors)
    done("render", f"{len(r.dropped_labels)} labels dropped")
    # The loom stage, not gtfs2graph: same stations and the same solved line
    # ordering, so only the shape differs. See animate.geographic_tracks. It
    # keeps a hidden line: the morph pairs the drawn tracks, which lack it.
    geo = None
    if feeds.get(key).geographic:
        geo_graph = LineGraph.from_geojson(paths["loom"]).reproject(to_mercator)
        geo = animate.geographic_tracks(geo_graph, graph, r, style)

    # A name for a line the map does not draw, hidden or unknown, is dropped.
    names = {label: chosen["name"] for label, chosen in (lines or {}).items()
             if "name" in chosen and label in labels}
    anim = animate.build(r, graph, trips, date, geo, line_order=line_order, names=names)
    done("animate", f"{len(anim.paths)} distinct paths"
         + (f", {len(anim.unrouted)} unrouted" if anim.unrouted else ""))

    out = out_dir or config.out_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{key}.svg").write_text(r.svg, encoding="utf-8", newline="\n")
    animate.write(anim, r.svg, out, stem=key, back=back, icons=icons,
                  social=social,
                  title=f"{name} — {service_day_text(date)}", name=name,
                  subtitle=f"{len(trips):,} trips · {service_day_text(date)}")
    # The progress message has always been the folder; the log line names
    # the files and never where they are (see ``_log``).
    done("write", str(out), f"{key}.svg, {key}.html and {key}.positions.json")

    return Result(key=key, date=date, layout=found.id, graph=graph, render=r, trips=trips,
                  match=match, animation=anim, paths=paths)


# -------------------------------------------------------- a layout's service day

@dataclass(frozen=True)
class Day:
    """A layout's service day as the schedule reads it: the octi stage its
    stops were matched on, unprojected, the match, the day and its trips."""

    date: dt.date
    graph: LineGraph
    match: StopMatch
    trips: list[Trip]


def schedule_for(found: Layout, date: dt.date | None, *, anchor: dt.date | None = None,
                 graph: LineGraph | None = None) -> Day:
    """The trips of a stored layout's lines on a day, read one way for
    ``run`` and for the minutes ``render.stage`` describes, so the two
    cannot drift. The feed is read as the layout was built from it, so an
    agency filter or a label rule the layout used applies here too, and its
    stops are matched against the octi stage unprojected. Without ``date``
    the busiest weekday is chosen scanning from ``anchor``, which is today
    unless the caller says otherwise; only ``run`` leaves it out. ``graph``
    is the octi stage when the caller has read it already. The tables are
    not kept."""
    graph = graph if graph is not None else LineGraph.from_geojson(found.paths["octi"])
    tables = feeds.tables(found.feed)
    lines = set(graph.labels)
    match = match_stops(graph, tables)
    date = date or busiest_weekday(tables, lines, anchor=anchor or dt.date.today())
    return Day(date=date, graph=graph, match=match, trips=trips_on(tables, date, match, lines))


# Each line's commonest trip on a day, timed (``schedule.line_runs``), per
# (layout id, day): the minutes render.stage's description gives. Plain values
# and nothing else, never the tables or the trips -- reading a day holds a
# feed's stop_times, 6.0 million rows and 2.5 GB on Chicago -- so a second
# description of the same layout and day reads no timetable, and map.build,
# which holds the day's trips already, fills it. Requests run on several
# workers: two misses at once may both read the day, which is harmless.
_MINUTES: dict[tuple[str, dt.date], dict[str, dict[str, Any] | None]] = {}
_minutes_lock = threading.Lock()


def line_minutes(found: Layout, date: dt.date, *,
                 graph: LineGraph | None = None) -> dict[str, dict[str, Any] | None]:
    """Every line of a stored layout timed by its commonest trip on ``date``:
    ``{label: {"minutes", "from", "to"} | None}``, the names the octi
    stage's. The day is read once per layout and day in a process. ``graph``
    is the octi stage when the caller has read it already, as it has for a
    layout still being laid out, whose octi file is not to be read from
    ``found``'s directory (``in_flight``)."""
    with _minutes_lock:
        known = _MINUTES.get((found.id, date))
    if known is not None:
        return known
    return _remember_minutes(found.id, schedule_for(found, date, graph=graph))


def _remember_minutes(layout: str, day: Day) -> dict[str, dict[str, Any] | None]:
    from .describe import station_name
    from .schedule import line_runs

    names = {node.id: station_name(node) for node in day.graph.stations}
    minutes = line_runs(day.trips, day.graph.labels, names)
    with _minutes_lock:
        _MINUTES[(layout, day.date)] = minutes
    return minutes
