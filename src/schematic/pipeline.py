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
finished. ``lay_out`` makes one, ``stored`` finds one, ``run`` draws from
one.

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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from . import __version__, animate, config, feeds, loom
from . import diagnostics as diagnostics_module
from .crs import to_mercator
from .linegraph import LineGraph
from .render import RenderResult, Style, render
from .schedule import (StopMatch, Trip, busiest_weekday, match_stops, trips_on)

# The LOOM stages, in order, with the arguments we run them with. Kept as data
# so a notebook can print the pipeline or re-run one stage with a tweak.
STAGES: list[tuple[str, tuple[str, ...]]] = [
    ("topo", ()),
    ("loom", ()),
    ("octi", ()),
]

# Called as each step finishes: (stage, fraction of the whole done, a line
# about what it produced). The JSON-RPC server turns these into notifications;
# a notebook can print them; nothing here waits on the callback.
Progress = Callable[[str, float, str], None]


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
        meta = json.loads(meta_path.read_text())
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
    while True:
        with _lock:
            in_flight = _building.get((key, layout))
            if in_flight is None:
                if not force:
                    existing = read_layout(key, layout) or _migrated(feed, layout, inputs)
                    if existing is not None:
                        for stage, path in existing.paths.items():
                            _report(progress, stage, _fraction(stage, stages), path)
                        return existing
                _building[(key, layout)] = threading.Event()
                break
        in_flight.wait()
    try:
        return _build(feed, layout, inputs, stages, progress)
    finally:
        with _lock:
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
    try:
        graph = loom.gtfs2graph(feeds.normalize(feed), "-m", feed.mode)
        out = scratch / STAGE_FILES["gtfs2graph"]
        out.write_text(json.dumps(graph))
        _report(progress, "gtfs2graph", _fraction("gtfs2graph", stages), out)
        payload = out.read_bytes()
        for tool, args in stages:
            payload = json.dumps(loom.run(tool, payload, *args)).encode()
            out = scratch / STAGE_FILES[tool]
            out.write_bytes(payload)
            _report(progress, tool, _fraction(tool, stages), out)
        meta = {**inputs, "engine": __version__,
                "made": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "migrated": False}
        (scratch / META_FILE).write_text(json.dumps(meta, indent=2) + "\n")
    except BaseException:
        # A cancel, a failure, an interrupt: the scratch goes, the stored
        # layout, if there was one, was never touched.
        shutil.rmtree(scratch, ignore_errors=True)
        raise
    try:
        with _lock:
            _swap(scratch, layout_dir(key, layout))
    except BaseException:
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
        (scratch / META_FILE).write_text(json.dumps(meta, indent=2) + "\n")
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


def _report(progress: Progress | None, stage: str, fraction: float, path: Path) -> None:
    if progress is not None:
        progress(stage, fraction, LineGraph.from_geojson(path).summary())


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
        line_order: list[str] | None = None, force: bool = False,
        out_dir: Path | None = None, back: str = "index.html",
        icons: str | None = None, social: str = "",
        progress: Progress | None = None) -> Result:
    """Everything: the layout, then draw, schedule, animate, write.

    ``layout`` names a stored layout to draw from, and then nothing is laid
    out: a missing one is refused. Without it the feed's registry entry is
    laid out if it is not stored yet (``force`` again), which is what the
    command line and the site want. ``date`` is the service day; without
    one the busiest weekday is chosen scanning from ``anchor``, which is
    today unless the caller says otherwise. ``back`` is the href the animation
    page's back-link points at. The default is the sibling gallery in
    ``out/``; the site passes its own atlas URL, because a relative
    "index.html" resolves to /maps/index.html there.
    """
    steps = ("gtfs2graph", "topo", "loom", "octi", "schedule", "render", "animate", "write")

    def tick(stage: str, message: str = "") -> None:
        if progress is not None:
            progress(stage, (steps.index(stage) + 1) / len(steps), message)

    if layout is not None:
        found = read_layout(key, layout)
        if found is None:
            raise LayoutMissing(f"{key} has no stored layout {layout[:8]}; lay the feed out "
                                f"first (graph.build)")
        if progress is not None:
            for stage, path in found.paths.items():
                _report(lambda st, _f, m: tick(st, m), stage, 0.0, path)
    else:
        found = lay_out(key, force=force,
                        progress=(lambda stage, _f, m: tick(stage, m)) if progress else None)
    paths = found.paths

    # Match stops against the unprojected graph -- station_id is what matters
    # there, and reprojecting is only needed for geometry.
    graph_ll = LineGraph.from_geojson(paths["octi"])
    require_edges(found.feed, graph_ll)
    graph = graph_ll.reproject(to_mercator)

    # The schedule reads the feed as the layout was built from it, so an
    # agency filter or a label rule the layout used applies here too.
    tables = feeds.tables(found.feed)
    lines = set(graph_ll.labels)
    match = match_stops(graph_ll, tables)
    date = date or busiest_weekday(tables, lines, anchor=anchor or dt.date.today())
    trips = trips_on(tables, date, match, lines)
    tick("schedule", f"{len(trips)} trips on {date:%A %-d %B %Y}; {match.report()}")

    name = feeds.get(key).name
    # Themed by default: the CSS variables carry literal fallbacks, so a
    # standalone SVG is unchanged, while an embedding page can theme the
    # furniture without resorting to an invert filter over the line colours.
    r = render(graph, width=width, style=style or Style(themed=True),
               title=name, line_order=line_order)
    tick("render", f"{len(r.dropped_labels)} labels dropped")
    # The loom stage, not gtfs2graph: same stations and the same solved line
    # ordering, so only the shape differs. See animate.geographic_tracks.
    geo = None
    if feeds.get(key).geographic:
        geo_graph = LineGraph.from_geojson(paths["loom"]).reproject(to_mercator)
        geo = animate.geographic_tracks(geo_graph, graph, r)

    anim = animate.build(r, graph, trips, date, geo, line_order=line_order)
    tick("animate", f"{len(anim.paths)} distinct paths"
         + (f", {len(anim.unrouted)} unrouted" if anim.unrouted else ""))

    out = out_dir or config.out_dir()
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{key}.svg").write_text(r.svg)
    animate.write(anim, r.svg, out, stem=key, back=back, icons=icons,
                  social=social,
                  title=f"{name} — {date:%A %-d %B %Y}", name=name,
                  subtitle=f"{len(trips):,} trips · {date:%A %-d %B %Y}")
    tick("write", str(out))

    return Result(key=key, date=date, layout=found.id, graph=graph, render=r, trips=trips,
                  match=match, animation=anim, paths=paths)
