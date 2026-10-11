"""Every reader of a stored layout reads it under the store's lock (engine
issue 77).

A forced re-layout's swap moves the stored set aside under
``pipeline._lock``, and Windows will not rename a directory while a file in
it is open: a reader that held a stored file open as the swap came failed
the swap, and the swap's failure removed the finished build. Issue 68 put
render.stage's reads under that lock; these are the others: a stored layout
found and replayed (``lay_out``), the map drawn from one (``pipeline.run``,
what map.build calls) and graph.build's answer. Each reads what it needs
whole in one hold of the lock and parses it outside.

As test_render_stage does, on every platform: the small timetabled feed
laid out by test_layouts' stand-in LOOM, ``os.replace`` stood in for by one
that refuses to move a directory while a file in it is open, as Windows
does, and the store's lock replaced by one that says when anyone has had to
wait for it.
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Any, Callable

import pytest
import test_layouts
from test_render_stage import DAY, Watched, longer_graph, small_layout, write_three_stop_feed
from test_serve import Client

from schematic import pipeline, render

KEY = "la-metro-rail"


def opened_under_the_lock(monkeypatch, folder: Path) -> list[tuple[str, bool]]:
    """Every file of ``folder`` opened from now on, by name, and whether the
    store's lock was held as it was opened."""
    opened: list[tuple[str, bool]] = []
    real = Path.open

    def watched(self, *args, **kwargs):
        if self.parent == folder:
            opened.append((self.name, pipeline._lock.locked()))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", watched)
    return opened


class Race:
    """A forced re-layout of ``made``, raced against a reader of the stored
    set that is held with one of its files open: the ``nth`` time the
    reader opens the ``held`` stage's file, or never when ``held`` is None.

    The reader is any thread but the test's own and the builder's, so a
    request answered on the server's own worker is a reader too.
    ``moved_on`` is set once anyone has had to wait for the store's lock, a
    rename has been refused, or the reader has ended."""

    def __init__(self, monkeypatch, made: pipeline.Layout, *, held: str | None = "octi",
                 nth: int = 1) -> None:
        self.monkeypatch = monkeypatch
        self.made = made
        self.held = made.paths[held] if held is not None else None
        self.nth = nth
        self.moved_on = threading.Event()
        self.reading, self.let_read = threading.Event(), threading.Event()
        # Holds of the build: just before its swap, and between the swap's
        # two renames, the stored set aside and the new one not yet in place.
        self.at_swap, self.let_swap = threading.Event(), threading.Event()
        self.in_gap, self.let_gap = threading.Event(), threading.Event()
        self.let_swap.set()
        self.let_gap.set()
        self.answers: dict[str, Any] = {}
        self.open_files: list[Path] = []
        self.opened = 0
        self.builder = threading.Thread(target=self._build, daemon=True)
        self.reader: threading.Thread | None = None
        monkeypatch.setattr(pipeline, "_lock", Watched(self.moved_on))
        # The swap's own retry of a refused rename would hide a reader that
        # held a file open outside the lock: here no rename is ever refused,
        # so the swap never waits to try one again.
        self.paused: list[float] = []
        monkeypatch.setattr(pipeline, "_pause", self.paused.append)
        real_open, real_replace = Path.open, os.replace

        @contextlib.contextmanager
        def held_open(path: Path, handle):
            with handle:
                self.open_files.append(path)
                try:
                    self.reading.set()
                    assert self.let_read.wait(30), "the test never let the read go"
                    yield handle
                finally:
                    self.open_files.remove(path)

        def open_(path, *args, **kwargs):
            handle = real_open(path, *args, **kwargs)
            if path == self.held and self._reading_thread():
                self.opened += 1
                if self.opened == self.nth:
                    return held_open(path, handle)
            return handle

        def replace(src, dst):
            if any(Path(src) in path.parents for path in self.open_files):
                self.moved_on.set()
                raise PermissionError(13, "The process cannot access the file because it is "
                                          "being used by another process", str(src))
            real_replace(src, dst)
            if Path(src) == self.made.dir and threading.current_thread() is self.builder:
                # The stored set is aside; the new one is not in place yet.
                self.in_gap.set()
                assert self.let_gap.wait(30), "the test never let the swap finish"

        monkeypatch.setattr(Path, "open", open_)
        monkeypatch.setattr(os, "replace", replace)

    def _reading_thread(self) -> bool:
        return threading.current_thread() not in (self.builder, threading.main_thread())

    def _build(self) -> None:
        try:
            self.answers["built"] = pipeline.lay_out(KEY, force=True)
        except BaseException as exc:  # handed to the test's thread, which asserts on it
            self.answers["build failed"] = exc

    def start_reader(self, read: Callable[[], Any]) -> None:
        def run() -> None:
            try:
                self.answers["read"] = read()
            except BaseException as exc:  # handed to the test's thread, which asserts on it
                self.answers["read failed"] = exc
            finally:
                # Nothing the test waits on outlives the reader.
                self.reading.set()
                self.moved_on.set()

        self.reader = threading.Thread(target=run, daemon=True)
        self.reader.start()

    def read_first(self, read: Callable[[], Any]) -> Any:
        """The reader opens a stored file and is held there; then a forced
        re-layout of the layout starts. It must wait for the lock, never
        reach its swap with the file open; and the reader is answered from
        the set it began on."""
        try:
            self.start_reader(read)
            assert self.reading.wait(30) and self.reader.is_alive(), \
                f"the reader never opened {self.held.name}: {self.answers}"
            self.builder.start()
            assert self.moved_on.wait(30), \
                "the re-layout neither waited for the lock nor was refused its swap"
        finally:
            self.let_read.set()
        return self._joined()

    def swap_first(self, read: Callable[[], Any]) -> Any:
        """A forced re-layout is held just before its swap; the reader opens
        a stored file and is held there; the swap is let go. It must wait for
        the lock rather than be refused its rename; and the reader is
        answered from the set it began on."""
        self.let_swap.clear()
        real_write = Path.write_text

        def write_text(path, *args, **kwargs):
            written = real_write(path, *args, **kwargs)
            if path.name == pipeline.META_FILE and ".building-" in path.parent.name:
                self.at_swap.set()  # the build's last write; the swap is next
                assert self.let_swap.wait(30), "the test never let the swap go"
            return written

        self.monkeypatch.setattr(Path, "write_text", write_text)
        try:
            self.builder.start()
            assert self.at_swap.wait(30), f"the re-layout never reached its swap: {self.answers}"
            self.start_reader(read)
            assert self.reading.wait(30) and self.reader.is_alive(), \
                f"the reader never opened {self.held.name}: {self.answers}"
            self.let_swap.set()
            assert self.moved_on.wait(30), "the swap neither waited for the lock nor was refused"
        finally:
            self.let_swap.set()
            self.let_read.set()
        return self._joined()

    def in_the_gap(self, read: Callable[[], Any] | None = None, *,
                   then: Callable[[], None] | None = None) -> Any:
        """A forced re-layout is held between its swap's two renames, the
        stored set moved aside and the new one not yet in its place; the
        reader starts there (``read``), or, started before and paused, is let
        on there (``then``). It must wait for the lock, never find the set
        missing (a ``FileNotFoundError``, or a layout not stored), and is
        answered from the new set once the swap is done."""
        self.let_gap.clear()
        self.let_read.set()
        try:
            self.builder.start()
            assert self.in_gap.wait(30), f"the re-layout never reached its swap: {self.answers}"
            if read is not None:
                self.start_reader(read)
            if then is not None:
                then()
            assert self.moved_on.wait(30), "the reader neither waited for the lock nor ended"
            assert self.reader.is_alive(), \
                f"the read ended between the swap's two renames: {self.answers}"
        finally:
            self.let_gap.set()
        return self._joined()

    def _joined(self) -> Any:
        self.builder.join(30)
        self.reader.join(30)
        assert not self.builder.is_alive() and not self.reader.is_alive()
        assert self.paused == [], f"the swap had a rename refused: {self.answers}"
        assert "build failed" not in self.answers, self.answers["build failed"]
        assert "read failed" not in self.answers, self.answers["read failed"]
        assert self.answers["built"].id == self.made.id
        assert test_layouts.ids_under(KEY) == [self.made.id], "nothing left aside or half made"
        return self.answers["read"]


@pytest.fixture
def client():
    c = Client()
    yield c
    c.endpoint.close()


def nodes_reported(reports: list[tuple[str, str]]) -> dict[str, str]:
    """Each stage's report, by stage, from its first word: how many nodes."""
    return {stage: message.split()[0] for stage, message in reports}


# ------------------------------------------------- a stored layout replayed

def test_a_stored_layout_is_replayed_from_files_read_under_the_lock(tmp_path, monkeypatch):
    """``lay_out`` of a layout that is stored reports each of its four
    stages, as if it had run them; the files those reports parse are read in
    the hold of the lock the layout was found in, the meta among them."""
    made = small_layout(tmp_path, monkeypatch)
    opened = opened_under_the_lock(monkeypatch, made.dir)
    reports: list[tuple[str, str]] = []
    again = pipeline.lay_out(KEY, progress=lambda stage, _f, m: reports.append((stage, m)))
    assert again == made
    assert [stage for stage, _message in reports] == list(pipeline.STAGE_FILES)
    assert {name for name, _held in opened} == {*pipeline.STAGE_FILES.values(),
                                                 pipeline.META_FILE}
    assert [name for name, held in opened if not held] == []


def test_a_forced_re_layout_waits_for_a_replay_still_reading_the_stored_set(
        tmp_path, monkeypatch):
    """The replay is held with the stored octi stage open, then a forced
    re-layout of the layout starts: it waits for the lock rather than reach
    its swap and be refused, and the replay reports the set it found."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    race = Race(monkeypatch, made)
    reports: list[tuple[str, str]] = []

    def replay() -> pipeline.Layout:
        return pipeline.lay_out(KEY, progress=lambda stage, _f, m: reports.append((stage, m)))

    assert race.read_first(replay) == made
    assert nodes_reported(reports) == dict.fromkeys(pipeline.STAGE_FILES, "2"), \
        "the set it found, not the re-layout's"
    reports.clear()
    replay()
    assert nodes_reported(reports) == dict.fromkeys(pipeline.STAGE_FILES, "3"), \
        "and the re-layout's set is the one stored"


# ------------------------------------------------ a map drawn from the store

def draw(made: pipeline.Layout, out: Path, *, by_id: bool = True,
         progress: bool = True) -> pipeline.Result:
    """map.build's draw of the small layout, as map.build makes it: by the
    layout's id, with its progress heard. ``by_id=False`` is the command
    line's, which lays the feed out first."""
    return pipeline.run(KEY, layout=made.id if by_id else None, date=DAY, out_dir=out,
                        progress=(lambda *_report: None) if progress else None)


@pytest.mark.parametrize("by_id, progress, read", [
    (True, True, {*pipeline.STAGE_FILES.values(), pipeline.META_FILE}),
    (True, False, {"02_loom.json", "03_octi.json", pipeline.META_FILE}),
    (False, True, {*pipeline.STAGE_FILES.values(), pipeline.META_FILE}),
    (False, False, {"02_loom.json", "03_octi.json", pipeline.META_FILE}),
], ids=["map.build", "by id, unheard", "laid out", "laid out, unheard"])
def test_every_file_of_the_store_a_map_reads_is_opened_under_the_lock(
        tmp_path, monkeypatch, by_id, progress, read):
    """The meta, the octi stage the map draws, the loom stage its
    geographic twin is drawn from, and, when a client hears its progress,
    the four stages it reports: each opened under the lock a forced
    re-layout's swap takes, by the layout's id as map.build draws and after
    laying the feed out as the command line does."""
    made = small_layout(tmp_path, monkeypatch)
    opened = opened_under_the_lock(monkeypatch, made.dir)
    result = draw(made, tmp_path / "out", by_id=by_id, progress=progress)
    assert result.layout == made.id
    assert {name for name, _held in opened} == read
    assert [name for name, held in opened if not held] == []


def test_a_forced_re_layouts_swap_never_finds_a_file_a_map_reads_open(tmp_path, monkeypatch):
    """Issue 77 as Windows has it, for map.build: a forced re-layout held
    just before its swap, a map of the stored set held with its octi stage
    open, the swap let go. It waits for the read; the map is drawn from the
    set it began on, and the next from the new set."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    result = Race(monkeypatch, made).swap_first(lambda: draw(made, tmp_path / "out"))
    assert len(result.graph.nodes) == 2, "the set it began on"
    assert len(draw(made, tmp_path / "out").graph.nodes) == 3, "and the new set after"


@pytest.mark.parametrize("reader", ["render.stage", "map.build", "map, laid out"])
def test_a_read_that_lands_between_the_swaps_renames_waits_for_the_new_set(
        tmp_path, monkeypatch, reader):
    """Issue 77: a forced re-layout's swap moves the stored set aside and
    then the new one into its place, two renames under the store's lock. A
    read between them found no set: a ``FileNotFoundError``, which reached
    a client as kind ``io`` with the folder's path in its hint, or the
    layout refused as not stored. Under the lock a read there waits, and is
    answered from the new set."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    race = Race(monkeypatch, made)
    if reader == "render.stage":
        _svg, counts, _description = race.in_the_gap(
            lambda: render.stage(KEY, "octi", layout=made.id, width=600))
        assert counts["nodes"] == 3
    else:
        result = race.in_the_gap(lambda: draw(made, tmp_path / "out",
                                              by_id=reader == "map.build"))
        assert len(result.graph.nodes) == 3


# ------------------------------------------------------- graph.build's answer

def octi_nodes(reply: dict) -> int:
    assert "result" in reply, reply
    return reply["result"]["stages"]["octi"]["nodes"]


def test_graph_build_answers_from_files_read_under_the_lock(tmp_path, monkeypatch, client):
    """graph.build of a stored layout reports its four stages from the
    replay and answers their counts and its meta from a read of its own:
    every file of the set either opens, it opens under the lock a forced
    re-layout's swap takes."""
    made = small_layout(tmp_path, monkeypatch)
    opened = opened_under_the_lock(monkeypatch, made.dir)
    reply = client.call("graph.build", {"key": KEY})
    assert reply["result"]["layout"] == made.id and reply["result"]["meta"] == made.meta
    names = [name for name, _held in opened]
    assert sorted(set(names)) == sorted({*pipeline.STAGE_FILES.values(), pipeline.META_FILE})
    assert names.count("03_octi.json") == 2, "the replay's report, then the answer's read"
    assert [name for name, held in opened if not held] == []


def test_a_forced_re_layout_waits_for_graph_builds_answer_still_reading_the_set(
        tmp_path, monkeypatch, client):
    """Issue 77 as Windows has it, for graph.build: its answer's read is
    held with the stored octi stage open (the second time it opens it; the
    first is the replay's report), then a forced re-layout starts. It waits
    for the lock rather than reach its swap and be refused, and graph.build
    answers the set it found."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    race = Race(monkeypatch, made, nth=2)
    assert octi_nodes(race.read_first(lambda: client.call("graph.build", {"key": KEY}))) == 2
    assert octi_nodes(client.call("graph.build", {"key": KEY})) == 3


def test_graph_builds_answer_read_between_the_swaps_renames_waits_for_the_new_set(
        tmp_path, monkeypatch, client):
    """Issue 77: graph.build has laid out (here, found the layout stored and
    replayed it) and is paused before it reads its answer; a forced
    re-layout is held between its swap's two renames; graph.build is let
    on. Its read waits for the lock rather than find the set missing, which
    answered a ``FileNotFoundError`` as kind ``io`` with the folder's path
    in its hint, and it answers the new set."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    race = Race(monkeypatch, made, held=None)
    laid, let_on = threading.Event(), threading.Event()
    real = pipeline.lay_out

    def paused(key, **kwargs):
        found = real(key, **kwargs)
        if not kwargs.get("force"):
            laid.set()
            assert let_on.wait(30), "the test never let graph.build go on"
        return found

    monkeypatch.setattr(pipeline, "lay_out", paused)
    race.start_reader(lambda: client.call("graph.build", {"key": KEY}))
    try:
        assert laid.wait(30), f"graph.build never laid out: {race.answers}"
        reply = race.in_the_gap(then=let_on.set)
    finally:
        let_on.set()
    assert octi_nodes(reply) == 3


# ------------------------------------------ a rename refused for a moment

def refusing(monkeypatch, made: pipeline.Layout, rename: str, times: int | None,
             error: OSError) -> list[tuple[str, str]]:
    """``os.replace`` raising ``error`` for one of a forced re-layout's two
    renames of ``made`` -- ``aside``, the stored set moved out of its place,
    or ``into place``, the new set moved in -- ``times`` times, or every
    time when None, as a virus scanner or the file explorer holding a file
    of it open for a moment makes Windows do. Every other rename is let
    through. The renames refused are listed."""
    refused: list[tuple[str, str]] = []
    real = os.replace

    def replace(src, dst):
        src, dst = Path(src), Path(dst)
        which = ("aside" if src == made.dir else
                 "into place" if dst == made.dir and ".building-" in src.name else None)
        if which == rename and (times is None or len(refused) < times):
            refused.append((src.name, dst.name))
            raise error
        return real(src, dst)

    monkeypatch.setattr(os, "replace", replace)
    return refused


def refusal() -> PermissionError:
    return PermissionError(13, "The process cannot access the file because it is being used "
                               "by another process")


@pytest.mark.parametrize("rename", ["aside", "into place"])
def test_a_swap_refused_for_a_moment_tries_the_rename_again(tmp_path, monkeypatch, rename):
    """Issue 77, for what the lock cannot cover: another process with a file
    of the set open for a moment. The swap tries the refused rename again,
    a short wait apart, and the re-layout is stored rather than given up."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    paused: list[float] = []
    monkeypatch.setattr(pipeline, "_pause", paused.append)
    refused = refusing(monkeypatch, made, rename, 2, refusal())
    assert pipeline.lay_out(KEY, force=True).id == made.id
    assert len(refused) == 2
    assert paused == [pipeline._SWAP_WAIT] * 2
    assert test_layouts.ids_under(KEY) == [made.id], "nothing left aside or half made"
    _found, files = pipeline.read_stored(KEY, made.id, ["octi"])
    assert len(pipeline._parsed(files["octi"]).nodes) == 3, "the new set is the one stored"


@pytest.mark.parametrize("error, tries", [
    (refusal(), pipeline._SWAP_TRIES),
    (OSError(16, "Device or resource busy"), 1),
], ids=["refused", "another error"])
def test_a_swap_refused_for_good_gives_the_build_up_as_before(tmp_path, monkeypatch, error,
                                                              tries):
    """A rename refused every time is tried ``_SWAP_TRIES`` times and then
    the refusal is the build's failure, as it was before the retry: the
    finished build is removed and the stored set is untouched. An error that
    is not a refusal is not tried again at all."""
    made = small_layout(tmp_path, monkeypatch, write=write_three_stop_feed)
    before = {path.name: path.read_bytes() for path in made.dir.iterdir()}
    monkeypatch.setattr(test_layouts, "GRAPH", longer_graph())
    paused: list[float] = []
    monkeypatch.setattr(pipeline, "_pause", paused.append)
    refused = refusing(monkeypatch, made, "aside", None, error)
    with pytest.raises(type(error)) as raised:
        pipeline.lay_out(KEY, force=True)
    assert raised.value is error
    assert len(refused) == tries
    assert paused == [pipeline._SWAP_WAIT] * (tries - 1)
    assert test_layouts.ids_under(KEY) == [made.id], "the build is removed"
    assert {path.name: path.read_bytes() for path in made.dir.iterdir()} == before
    assert pipeline._building == {} and pipeline._scratches == {}
