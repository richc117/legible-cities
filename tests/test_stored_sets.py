"""A stored layout is never lost to a crash (engine issue 83).

A forced re-layout swaps its new set in with two renames under the store's
lock: the stored set aside as ``<id>.old-<pid>``, then the new set into its
place. A process ended between the two -- a crash, or the desktop app's
``taskkill /F`` at quit -- leaves the layout aside and its place empty, and
a layout cannot be made again. On Windows ``_alive`` cannot ask whether a
process is gone and presumes it is not, so that set was never put back: the
project's layout read as not stored. It goes back now whoever set it aside,
before anything asks whether the layout is stored, and in a build's sweep.

LOOM is test_layouts' stand-in. Windows is simulated by handing ``pipeline``
an ``os`` whose ``name`` is ``"nt"``, the only thing ``_alive`` asks; the
platform's own ``os`` is untouched, so paths and renames behave as they do
here. A process that is gone is a child already waited for, never a number
taken to be free.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

from test_layouts import KEY, FakeLoom, ids_under, write_timetabled_feed

from schematic import config, feeds, loom, pipeline


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch home with test_layouts' small feed in place of the download."""
    monkeypatch.setenv(config.ENV, str(tmp_path))
    monkeypatch.delenv(loom.COMMIT_ENV, raising=False)
    monkeypatch.delenv(loom.BIN_ENV, raising=False)
    zipped = feeds.FEEDS[KEY].zip_path
    zipped.parent.mkdir(parents=True)
    write_timetabled_feed(zipped)
    return tmp_path


def as_windows(monkeypatch) -> None:
    """``pipeline``'s ``os`` as Windows has it: ``name`` is ``"nt"``, and
    everything else is the platform's own ``os``, looked up as it is used."""

    class Windows(types.ModuleType):
        def __getattr__(self, name: str):
            return getattr(os, name)

    windows = Windows("os")
    windows.name = "nt"
    monkeypatch.setattr(pipeline, "os", windows)


def gone() -> int:
    """The pid of a process that has ended: a child, already waited for."""
    child = subprocess.Popen([sys.executable, "-c", ""], stdin=subprocess.DEVNULL)
    assert child.wait(timeout=30) == 0
    return child.pid


@contextlib.contextmanager
def running():
    """The pid of a process that is running for as long as the block does."""
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                             stdin=subprocess.DEVNULL)
    try:
        yield child.pid
    finally:
        child.kill()
        child.wait(timeout=30)


@contextlib.contextmanager
def writer(which: str):
    """The pid a leftover is named with: a process that is gone, this one,
    or another that is running."""
    if which == "gone":
        yield gone()
    elif which == "this process":
        yield os.getpid()
    else:
        with running() as pid:
            yield pid


def files_of(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in folder.iterdir()}


# ------------------------------------------- a set left aside, its place empty

DOORS = {
    "stored": lambda layout: pipeline.stored(KEY),
    "read_stored": lambda layout: pipeline.read_stored(KEY, layout, ["octi"])[0],
    "lay_out": lambda layout: pipeline.lay_out(KEY),
    "lay_out, forced": lambda layout: pipeline.lay_out(KEY, force=True),
}


@pytest.mark.parametrize("door", list(DOORS))
@pytest.mark.parametrize("by", ["gone", "this process", "running"])
@pytest.mark.parametrize("platform", ["here", "windows"])
def test_a_set_left_aside_with_its_place_empty_is_put_back_before_it_is_looked_for(
        home, monkeypatch, door, by, platform):
    """A swap interrupted between its renames: the layout aside, its place
    empty. Whatever asks next for it -- ``stored``, ``read_stored`` (what
    map.build and render.stage read through), ``lay_out`` -- finds it,
    byte for byte, on Windows as here and whether the process that put it
    aside is gone or not, and nothing is laid out again. A forced re-layout
    finds it in its place too, and replaces it whole, leaving nothing
    aside."""
    fake = FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    before = files_of(made.dir)
    if platform == "windows":
        as_windows(monkeypatch)
    with writer(by) as pid:
        made.dir.rename(made.dir.with_name(f"{made.id}.old-{pid}"))
        assert pipeline.read_layout(KEY, made.id) is None, "the layout reads as not stored"
        fake.calls.clear()
        found = DOORS[door](made.id)
    assert found.id == made.id
    assert ids_under(KEY) == [made.id], "nothing is left aside"
    if door == "lay_out, forced":
        assert fake.calls, "a forced re-layout lays the feed out again"
        assert files_of(made.dir).keys() == before.keys()
    else:
        assert fake.calls == [], "nothing is laid out again"
        assert files_of(made.dir) == before, "the set it was"


@pytest.mark.parametrize("platform", ["here", "windows"])
def test_a_build_sweeps_a_set_left_aside_with_its_place_empty_back_into_it(
        home, monkeypatch, platform):
    """Another layout of the feed being built sweeps the folder they share,
    and the set a swap left aside goes back into its place there too,
    before anything has asked for it."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    before = files_of(made.dir)
    if platform == "windows":
        as_windows(monkeypatch)
    made.dir.rename(made.dir.with_name(f"{made.id}.old-{gone()}"))
    other = pipeline.lay_out(KEY, mode="tram")
    assert ids_under(KEY) == sorted([made.id, other.id])
    assert files_of(made.dir) == before


# ------------------------------------------- what a sweep removes, and what not

def leftovers(made: pipeline.Layout, pid: int) -> tuple[Path, Path]:
    """Beside the stored set: an older set put aside by ``pid``, its place
    holding a set again, as a swap leaves it just before it removes it; and
    a build's scratch of ``pid``'s."""
    aside = made.dir.with_name(f"{made.id}.old-{pid}")
    shutil.copytree(made.dir, aside)
    (aside / pipeline.STAGE_FILES["octi"]).write_text('{"set": "older"}')
    scratch = made.dir.with_name(f"{made.id}.building-{pid}-x")
    scratch.mkdir()
    (scratch / pipeline.STAGE_FILES["gtfs2graph"]).write_text("{}")
    return aside, scratch


@pytest.mark.skipif(os.name == "nt", reason="a process that is gone is presumed alive there")
def test_a_set_aside_whose_place_holds_a_set_goes_when_its_writer_has(home, monkeypatch):
    """Cleaned as before: a set put aside whose place holds a set again is
    not put back over it, and goes with its process; so does that process's
    scratch. The stored set is untouched."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    before = files_of(made.dir)
    aside, scratch = leftovers(made, gone())
    other = pipeline.lay_out(KEY, mode="tram")
    assert not aside.exists() and not scratch.exists()
    assert ids_under(KEY) == sorted([made.id, other.id])
    assert files_of(made.dir) == before


@pytest.mark.parametrize("by", ["running", "gone, on windows"])
def test_a_live_writers_leftovers_are_left_alone(home, monkeypatch, by):
    """A set put aside whose place holds a set, and a build's scratch, of a
    process that is running -- or, on Windows, of any process but this
    one, since it cannot be asked -- are that process's: a sweep leaves
    both as they are, and the stored set too."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    before = files_of(made.dir)
    with contextlib.ExitStack() as stack:
        if by == "running":
            pid = stack.enter_context(running())
        else:
            pid = gone()
            as_windows(monkeypatch)
        aside, scratch = leftovers(made, pid)
        kept = files_of(aside), files_of(scratch)
        other = pipeline.lay_out(KEY, mode="tram")
        assert (files_of(aside), files_of(scratch)) == kept
    assert ids_under(KEY) == sorted([made.id, other.id, aside.name, scratch.name])
    assert files_of(made.dir) == before
