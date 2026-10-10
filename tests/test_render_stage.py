"""render.stage: a stored stage graph, drawn, with its counts (E15).

Gated on the checkout's cached Los Angeles layout, as test_cdmx is on its
own; the missing-stage case needs nothing stored, and neither do the reads
of the store against a forced re-layout's swap (engine issue 68), which lay
test_layouts' small feed out with its stand-in LOOM.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import threading
from pathlib import Path

import pytest

import test_layouts
from test_layouts import FakeLoom, ids_under, write_timetabled_feed

from schematic import config, feeds, loom, pipeline, render
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph

KEY = "la-metro-rail"

needs_la = pytest.mark.skipif(pipeline.stage_path(KEY, "octi") is None,
                              reason="run the pipeline once to populate data/graphs")


@needs_la
@pytest.mark.parametrize("stage", ["gtfs2graph", "loom", "octi"])
def test_a_stage_renders_with_the_counts_its_graph_has(stage):
    svg, counts, _ = render.stage(KEY, stage)
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
    svg_a, counts_a, _ = render.stage(KEY, "gtfs2graph", layout=stored.id, width=600)
    svg_b, counts_b, _ = render.stage(KEY, "gtfs2graph", width=600)
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


# ------------------------------------- the store's reads and a forced re-layout

DAY = dt.date(2026, 9, 10)


def small_layout(tmp_path, monkeypatch) -> pipeline.Layout:
    """test_layouts' small timetabled feed, laid out by its stand-in LOOM in
    a home of its own, with an empty cache of minutes."""
    monkeypatch.setenv(config.ENV, str(tmp_path))
    monkeypatch.delenv(loom.COMMIT_ENV, raising=False)
    monkeypatch.delenv(loom.BIN_ENV, raising=False)
    monkeypatch.setattr(pipeline, "_MINUTES", {})
    zipped = feeds.FEEDS[KEY].zip_path
    zipped.parent.mkdir(parents=True)
    write_timetabled_feed(zipped)
    FakeLoom(monkeypatch)
    return pipeline.lay_out(KEY)


def moved_graph() -> dict:
    """test_layouts' graph with station B somewhere else: a forced re-layout
    under the same id that draws differently, as ``topo`` may."""
    moved = json.loads(json.dumps(test_layouts.GRAPH))
    moved["features"][1]["geometry"]["coordinates"] = [-118.6, 34.1]
    moved["features"][2]["geometry"]["coordinates"][1] = [-118.6, 34.1]
    return moved


def test_every_file_of_the_store_render_stage_opens_is_opened_under_the_lock(
        tmp_path, monkeypatch):
    """Engine issue 68, on every platform: the stage, the meta beside it and
    the octi stage a date's minutes are read from are each opened while the
    store's lock is held, the lock a forced re-layout's swap takes; by the
    layout's id and by the registry entry alike."""
    made = small_layout(tmp_path, monkeypatch)
    opened: list[tuple[str, bool]] = []
    real = Path.open

    def watched(self, *args, **kwargs):
        if self.parent == made.dir:
            opened.append((self.name, pipeline._lock.locked()))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", watched)
    for stage in pipeline.STAGE_FILES:
        render.stage(KEY, stage, layout=made.id, width=600)
        render.stage(KEY, stage, width=600, date=DAY)
    assert {name for name, _held in opened} == {*pipeline.STAGE_FILES.values(),
                                                 pipeline.META_FILE}
    assert [name for name, held in opened if not held] == []


class Watched:
    """The store's lock, saying when anyone has had to wait for it."""

    def __init__(self, waited: threading.Event) -> None:
        self.inner = threading.Lock()
        self.waited = waited

    def __enter__(self) -> Watched:
        if not self.inner.acquire(blocking=False):
            self.waited.set()
            self.inner.acquire()
        return self

    def __exit__(self, *_exc) -> None:
        self.inner.release()

    def locked(self) -> bool:
        return self.inner.locked()


def test_a_forced_re_layouts_swap_never_finds_a_stored_stage_open(tmp_path, monkeypatch):
    """Engine issue 68 as Windows has it, on every platform: ``os.replace``
    is stood in for by one that refuses to move a directory while a file in
    it is open, as Windows does. A forced re-layout is held just before its
    swap, a render.stage of the stored set is held with its stage file open,
    and the swap is let go: it waits for the read rather than failing the
    build, and the read is answered from the set it began on."""
    made = small_layout(tmp_path, monkeypatch)
    before = render.stage(KEY, "gtfs2graph", layout=made.id, width=600)
    monkeypatch.setattr(test_layouts, "GRAPH", moved_graph())
    stage_file = made.paths["gtfs2graph"]
    at_swap, let_swap = threading.Event(), threading.Event()
    reading, let_read = threading.Event(), threading.Event()
    # Set once the swap has had to wait for the lock, or has been refused.
    moved_on = threading.Event()
    monkeypatch.setattr(pipeline, "_lock", Watched(moved_on))
    open_files: list[Path] = []
    answers: dict[str, object] = {}

    def build() -> None:
        try:
            answers["built"] = pipeline.lay_out(KEY, force=True)
        except BaseException as exc:  # handed to the test's thread, which asserts on it
            answers["build failed"] = exc

    def draw() -> None:
        try:
            answers["drawn"] = render.stage(KEY, "gtfs2graph", layout=made.id, width=600)
        except BaseException as exc:
            answers["draw failed"] = exc

    builder = threading.Thread(target=build, daemon=True)
    reader = threading.Thread(target=draw, daemon=True)
    real_write, real_open, real_replace = Path.write_text, Path.open, os.replace

    def write_text(self, *args, **kwargs):
        written = real_write(self, *args, **kwargs)
        if self.name == pipeline.META_FILE and ".building-" in self.parent.name:
            at_swap.set()  # the build's last write; the swap is next
            assert let_swap.wait(30), "the test never let the swap go"
        return written

    @contextlib.contextmanager
    def held_open(path: Path, handle):
        with handle:
            open_files.append(path)
            try:
                reading.set()
                assert let_read.wait(30), "the test never let the read go"
                yield handle
            finally:
                open_files.remove(path)

    def open_(self, *args, **kwargs):
        handle = real_open(self, *args, **kwargs)
        if self == stage_file and threading.current_thread() is reader:
            return held_open(self, handle)
        return handle

    def replace(src, dst):
        if any(Path(src) in path.parents for path in open_files):
            moved_on.set()
            raise PermissionError(13, "The process cannot access the file because it is "
                                      "being used by another process", str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(Path, "open", open_)
    monkeypatch.setattr(os, "replace", replace)
    builder.start()
    assert at_swap.wait(30), "the forced re-layout never reached its swap"
    reader.start()
    assert reading.wait(30), "render.stage never opened the stored stage"
    let_swap.set()
    assert moved_on.wait(30), "the swap neither waited for the lock nor was refused"
    let_read.set()
    builder.join(30)
    reader.join(30)
    assert not builder.is_alive() and not reader.is_alive()
    assert "build failed" not in answers, answers["build failed"]
    assert "draw failed" not in answers, answers["draw failed"]
    assert answers["drawn"] == before, "read whole from the set it began on"
    assert answers["built"].id == made.id and ids_under(KEY) == [made.id]
    after = render.stage(KEY, "gtfs2graph", layout=made.id, width=600)
    assert after[0] != before[0], "and the new set is the one stored"
