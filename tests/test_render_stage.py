"""render.stage: a stored stage graph, drawn, with its counts (E15).

Gated on the checkout's cached Los Angeles layout, as test_cdmx is on its
own; the missing-stage case needs nothing stored, and neither do the reads
of the store against a forced re-layout's swap (engine issue 68) and the
minutes after it (engine issue 69), which lay test_layouts' small feed out
with its stand-in LOOM.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import threading
import zipfile
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


def small_layout(tmp_path, monkeypatch, write=write_timetabled_feed) -> pipeline.Layout:
    """test_layouts' small timetabled feed, or the one ``write`` writes, laid
    out by its stand-in LOOM in a home of its own, with an empty cache of
    minutes."""
    monkeypatch.setenv(config.ENV, str(tmp_path))
    monkeypatch.delenv(loom.COMMIT_ENV, raising=False)
    monkeypatch.delenv(loom.BIN_ENV, raising=False)
    monkeypatch.setattr(pipeline, "_MINUTES", {})
    zipped = feeds.FEEDS[KEY].zip_path
    zipped.parent.mkdir(parents=True)
    write(zipped)
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


def test_the_handler_reads_a_stored_sets_meta_under_the_lock_while_it_is_rebuilt(
        tmp_path, monkeypatch):
    """Engine issue 68 on render.stage's own handler: while a forced re-layout
    of the layout is running (``in_flight`` says the stage is not yet), the
    stored set answers, and the meta that says it is whole is opened under the
    lock a swap takes, not before it."""
    from schematic import serve

    made = small_layout(tmp_path, monkeypatch)
    opened: list[tuple[str, bool]] = []
    real = Path.open

    def watched(self, *args, **kwargs):
        if self.parent == made.dir:
            opened.append((self.name, pipeline._lock.locked()))
        return real(self, *args, **kwargs)

    def not_yet(key, layout, needed):
        raise pipeline.NotYet(key, layout, "octi")

    monkeypatch.setattr(pipeline, "in_flight", not_yet)
    monkeypatch.setattr(Path, "open", watched)
    svg, _counts, _description = serve._drawn_stage(
        KEY, made.id, "loom", width=600, labels=True, date=None)
    assert svg.startswith("<svg")
    names = [name for name, _held in opened]
    assert pipeline.META_FILE in names
    assert [name for name, held in opened if not held] == []


def test_a_graph_the_caller_read_is_not_kept_as_a_stored_sets_minutes(tmp_path, monkeypatch):
    """Engine issue 69: ``line_minutes`` of a stored layout reads the octi
    stage itself, after taking the epoch, and ignores a graph handed in, so
    one read before a swap cannot be kept as the new set's minutes."""
    made = small_layout(tmp_path, monkeypatch)
    wrong = LineGraph.from_geojson(
        json.loads(made.paths["octi"].read_text(encoding="utf-8")))
    wrong.edges.clear()
    on_disk = pipeline.line_minutes(made, DAY)
    pipeline._MINUTES.clear()
    assert pipeline.line_minutes(made, DAY, graph=wrong) == on_disk


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


# ------------------------------- the minutes after a forced re-layout (issue 69)

def write_three_stop_feed(path: Path) -> None:
    """test_layouts' timetabled feed with a third stop: the train leaves A at
    08:00, calls at B at 08:10 and runs on to C at 08:25, every day. A line
    drawn from A to B is timed at 10 minutes; drawn on to C, at 25."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("agency.txt", "agency_id,agency_name,agency_timezone\n"
                                  "LACMTA,Metro,America/Los_Angeles\n")
        zf.writestr("routes.txt", "route_id,agency_id,route_short_name,route_long_name,route_type\n"
                                  "r1,LACMTA,A,Metro A Line,0\n")
        zf.writestr("calendar.txt",
                    "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,"
                    "start_date,end_date\ns1,1,1,1,1,1,1,1,20260101,20271231\n")
        zf.writestr("trips.txt", "route_id,service_id,trip_id\nr1,s1,t1\n")
        zf.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\n"
                                 "a,A,34.0,-118.2\nb,B,34.1,-118.3\nc,C,34.2,-118.4\n")
        zf.writestr("stop_times.txt", "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
                                      "t1,08:00:00,08:00:00,a,1\nt1,08:10:00,08:10:00,b,2\n"
                                      "t1,08:25:00,08:25:00,c,3\n")


def longer_graph() -> dict:
    """test_layouts' graph with line A drawn on from B to a third station,
    C: a forced re-layout under the same id whose octi stage times the line
    longer, as ``topo``, which is not reproducible, may."""
    graph = json.loads(json.dumps(test_layouts.GRAPH))
    graph["features"] += [
        {"type": "Feature", "properties": {"id": "n3", "station_id": "c", "station_label": "C"},
         "geometry": {"type": "Point", "coordinates": [-118.4, 34.2]}},
        {"type": "Feature", "properties": {"from": "n2", "to": "n3",
                                           "lines": [{"id": "l1", "label": "A", "color": "ff0000"}]},
         "geometry": {"type": "LineString", "coordinates": [[-118.3, 34.1], [-118.4, 34.2]]}}]
    return graph


# Line A's minutes and the two ends it is timed between, which the
# description names in the stage's own direction.
TO_B = (10, {"A", "B"})
TO_C = (25, {"A", "C"})


def run_of(trip: dict) -> tuple[int, set[str]]:
    return trip["minutes"], {trip["from"], trip["to"]}


def timed(layout: str, date: dt.date = DAY) -> tuple[int, set[str]]:
    """Line A's run in render.stage's description of the stored octi stage."""
    _svg, _counts, description = render.stage(KEY, "octi", layout=layout, width=600, date=date)
    return run_of({line["label"]: line["trip"] for line in description["lines"]}["A"])


def test_after_a_forced_re_layout_the_minutes_are_read_from_the_new_octi_stage(
        tmp_path, monkeypatch):
    """Engine issue 69: the minutes are kept by layout id and day, and a
    forced re-layout keeps the id; the swap forgets them, so a description
    after it times the line as the new octi stage draws it."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    assert timed(made.id) == TO_B
    assert timed(made.id) == TO_B, "kept, and answered again"
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    assert pipeline.lay_out(KEY, force=True).id == made.id
    assert timed(made.id) == TO_C


@pytest.mark.parametrize("reader", ["description", "map"])
def test_a_day_read_from_the_old_set_that_ends_after_the_swap_is_not_kept(
        tmp_path, monkeypatch, reader):
    """Engine issue 69, the read across the swap: a description with a date,
    or a map, reads the old octi stage, and its day read is held while a
    forced re-layout swaps the new set in. It is answered from the set it
    read, and its minutes are not kept afterwards as the new set's."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    reading, let_finish = threading.Event(), threading.Event()
    answers: dict[str, object] = {}
    real = pipeline.schedule_for

    def held(found, date, **kwargs):
        day = real(found, date, **kwargs)
        if threading.current_thread() is thread:
            reading.set()
            assert let_finish.wait(30), "the test never let the day read go"
        return day

    def read() -> None:
        try:
            if reader == "description":
                answers["trip"] = timed(made.id)
            else:
                pipeline.run(KEY, layout=made.id, date=DAY, out_dir=tmp_path / "out")
                answers["trip"] = "drawn"
        except BaseException as exc:  # handed to the test's thread, which asserts on it
            answers["failed"] = exc

    monkeypatch.setattr(pipeline, "schedule_for", held)
    thread = threading.Thread(target=read, daemon=True)
    thread.start()
    try:
        assert reading.wait(30), f"the day was never read: {answers}"
        assert pipeline.lay_out(KEY, force=True).id == made.id
    finally:
        let_finish.set()
    thread.join(30)
    assert not thread.is_alive()
    assert "failed" not in answers, answers["failed"]
    assert answers["trip"] == (TO_B if reader == "description" else "drawn"), \
        "answered from the set it read"
    assert timed(made.id) == TO_C


def test_a_layout_being_laid_out_again_is_timed_from_its_own_octi_stage_and_never_kept(
        tmp_path, monkeypatch):
    """Engine issue 69 before the swap: a forced re-layout whose octi stage
    is whole in its scratch is timed from that stage, not from the stored
    set's minutes kept under the same id; and what it is timed at is not
    kept, so a swap that then fails leaves the stored set timed as before."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    later = DAY + dt.timedelta(days=1)
    assert timed(made.id) == TO_B
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    at_swap, let_swap = threading.Event(), threading.Event()
    answers: dict[str, object] = {}
    real_write = Path.write_text

    def write_text(self, *args, **kwargs):
        written = real_write(self, *args, **kwargs)
        if self.name == pipeline.META_FILE and ".building-" in self.parent.name:
            at_swap.set()
            assert let_swap.wait(30), "the test never let the swap go"
        return written

    def refused(scratch, target):
        raise PermissionError(13, "The process cannot access the file because it is being "
                                  "used by another process", str(target))

    def build() -> None:
        try:
            answers["built"] = pipeline.lay_out(KEY, force=True)
        except BaseException as exc:  # handed to the test's thread, which asserts on it
            answers["build failed"] = exc

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(pipeline, "_swap", refused)
    builder = threading.Thread(target=build, daemon=True)
    builder.start()
    try:
        assert at_swap.wait(30), f"the forced re-layout never reached its swap: {answers}"
        found, files = pipeline.in_flight(KEY, made.id, ["octi"])
        octi = LineGraph.from_geojson(json.loads(files["octi"]))
        for date in (DAY, later):
            assert run_of(pipeline.line_minutes(found, date, graph=octi)["A"]) == TO_C, date
    finally:
        let_swap.set()
    builder.join(30)
    assert not builder.is_alive()
    assert isinstance(answers.get("build failed"), PermissionError), answers
    assert timed(made.id) == TO_B
    assert timed(made.id, later) == TO_B, "the scratch's minutes were not kept"
