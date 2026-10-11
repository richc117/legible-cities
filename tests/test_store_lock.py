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
    reader opens the ``held`` stage's file.

    The reader is any thread but the test's own and the builder's, so a
    request answered on the server's own worker is a reader too.
    ``moved_on`` is set once anyone has had to wait for the store's lock, a
    rename has been refused, or the reader has ended."""

    def __init__(self, monkeypatch, made: pipeline.Layout, *, held: str = "octi",
                 nth: int = 1) -> None:
        self.monkeypatch = monkeypatch
        self.made = made
        self.held = made.paths[held]
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

    def _start_reader(self, read: Callable[[], Any]) -> None:
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
            self._start_reader(read)
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
            self._start_reader(read)
            assert self.reading.wait(30) and self.reader.is_alive(), \
                f"the reader never opened {self.held.name}: {self.answers}"
            self.let_swap.set()
            assert self.moved_on.wait(30), "the swap neither waited for the lock nor was refused"
        finally:
            self.let_swap.set()
            self.let_read.set()
        return self._joined()

    def in_the_gap(self, read: Callable[[], Any]) -> Any:
        """A forced re-layout is held between its swap's two renames, the
        stored set moved aside and the new one not yet in its place; the
        reader starts there. It must wait for the lock, never find the set
        missing (a ``FileNotFoundError``, or a layout not stored), and is
        answered from the new set once the swap is done."""
        self.let_gap.clear()
        self.let_read.set()
        try:
            self.builder.start()
            assert self.in_gap.wait(30), f"the re-layout never reached its swap: {self.answers}"
            self._start_reader(read)
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
        assert "build failed" not in self.answers, self.answers["build failed"]
        assert "read failed" not in self.answers, self.answers["read failed"]
        assert self.answers["built"].id == self.made.id
        assert test_layouts.ids_under(KEY) == [self.made.id], "nothing left aside or half made"
        return self.answers["read"]


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
