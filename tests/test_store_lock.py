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

import test_layouts
from test_render_stage import Watched, longer_graph, small_layout, write_three_stop_feed

from schematic import pipeline

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
        self.made = made
        self.held = made.paths[held]
        self.nth = nth
        self.moved_on = threading.Event()
        self.reading, self.let_read = threading.Event(), threading.Event()
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
            return real_replace(src, dst)

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
