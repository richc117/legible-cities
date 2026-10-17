"""A stored layout is never lost to a crash, and never deleted from under a
move of it (engine issue 83).

A forced re-layout swaps its new set in with two renames under the store's
lock: the stored set aside as ``<id>.old-<pid>``, then the new set into its
place. A process ended between the two -- a crash, or the desktop app's
``taskkill /F`` at quit -- leaves the layout aside and its place empty, and
a layout cannot be made again. On Windows ``_alive`` cannot ask whether a
process is gone and presumes it is not, so that set was never put back: the
project's layout read as not stored. It goes back now whoever set it aside,
before anything asks whether the layout is stored, and in a build's sweep.

``feeds.remove`` deleted a feed's layouts without the store's lock, so the
deletion could land between a forced re-layout's two renames.

LOOM is test_layouts' stand-in. Windows is simulated by handing ``pipeline``
an ``os`` whose ``name`` is ``"nt"``, the only thing ``_alive`` asks; the
platform's own ``os`` is untouched, so paths and renames behave as they do
here. A process that is gone is a child already waited for, never a number
taken to be free.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from test_layouts import KEY, FakeLoom, ids_under, write_timetabled_feed
from test_render_stage import Watched
from test_serve import Client

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


# ------------------------------------ which set goes back, and what is no leftover

def two_aside(made: pipeline.Layout, pids: tuple[int, int], by: str) -> tuple[Path, Path]:
    """The stored set put aside twice, by ``pids`` in that order, its place
    empty: the second is the newer, by its meta's ``made`` or, with no
    ``made`` in either meta, by when its folder last changed. Each says
    which it is in a file of its own."""
    asides = [made.dir.with_name(f"{made.id}.old-{pid}") for pid in pids]
    for aside, which, when in zip(asides, ("older", "newer"), ("2026-10-01", "2026-10-02")):
        shutil.copytree(made.dir, aside)
        meta = json.loads((aside / pipeline.META_FILE).read_text())
        if by == "made":
            meta["made"] = f"{when}T00:00:00+00:00"
        else:
            del meta["made"]
        (aside / pipeline.META_FILE).write_text(json.dumps(meta))
        (aside / "which").write_text(which)
    shutil.rmtree(made.dir)
    if by == "mtime":
        os.utime(asides[0], (1_700_000_000, 1_700_000_000))
        os.utime(asides[1], (1_800_000_000, 1_800_000_000))
    return asides[0], asides[1]


@pytest.mark.parametrize("by", ["made", "mtime"])
def test_of_two_sets_left_aside_the_newest_goes_back_not_the_first_by_name(
        home, monkeypatch, by):
    """Two sets left aside for one id, the newer named ``.old-200`` and the
    older ``.old-1000``, which comes first by name: the newer goes back,
    and the older is left aside for a sweep, which removes it once its
    writer is gone."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    older, newer = two_aside(made, (1000, 200), by)
    assert sorted([older.name, newer.name])[0] == older.name, "the older comes first by name"
    found = pipeline.stored(KEY)
    assert found.id == made.id
    assert (made.dir / "which").read_text() == "newer"
    assert older.is_dir() and not newer.exists()


def test_a_sweep_puts_the_newest_set_back_and_removes_the_older(home, monkeypatch):
    """The same in a build's sweep, of writers that are gone: the newer set
    goes back, whichever comes first by name, and the older goes."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    first, second = sorted((gone(), gone()), key=lambda pid: f"{made.id}.old-{pid}")
    older, newer = two_aside(made, (first, second), "made")
    other = pipeline.lay_out(KEY, mode="tram")
    assert (made.dir / "which").read_text() == "newer"
    assert ids_under(KEY) == sorted([made.id, other.id])


# A set that is not whole, each way ``_whole_meta`` tells: its meta cut off
# as a crash mid-write leaves it, a stage file gone, or a meta nested deeper
# than the parser goes, which raises a RecursionError.
DAMAGES = ("a truncated meta", "a stage file missing", "a meta nested past the parser")


def damage(folder: Path, how: str) -> None:
    meta = folder / pipeline.META_FILE
    if how == "a truncated meta":
        meta.write_text(meta.read_text()[:20])
    elif how == "a stage file missing":
        (folder / pipeline.STAGE_FILES["octi"]).unlink()
    else:
        meta.write_text("[" * 100_000 + "]" * 100_000)


LOOKUPS = {"stored": lambda layout: pipeline.stored(KEY),
           "read_stored": lambda layout: (pipeline.read_stored(KEY, layout, []) or (None,))[0]}


@pytest.mark.parametrize("door", list(LOOKUPS))
@pytest.mark.parametrize("how", DAMAGES)
def test_a_damaged_newest_set_aside_never_outranks_a_whole_older_one(home, monkeypatch, how,
                                                                     door):
    """The newer of two sets left aside is damaged. Ranked by when it was
    made, or by when its folder changed where its meta cannot be read, it
    would go back and read as not stored, and the whole older set beside it
    would go to a sweep once the place held a set: a layout lost. The whole
    one goes back; the damaged one stays aside, and nothing raises."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    older, newer = two_aside(made, (1000, 200), "made")
    damage(newer, how)
    kept = files_of(newer)
    found = LOOKUPS[door](made.id)
    assert found is not None and found.id == made.id
    assert (made.dir / "which").read_text() == "older"
    assert files_of(newer) == kept and not older.exists()


@pytest.mark.parametrize("how", DAMAGES)
def test_where_no_set_aside_is_whole_nothing_moves_and_a_sweep_keeps_them(home, monkeypatch,
                                                                          how):
    """Every set left aside for the layout is damaged: none goes into the
    place, a lookup answers not stored without raising, and a sweep by a
    build beside it, their writers gone, keeps every one as it was, to be
    looked at."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    asides = two_aside(made, (gone(), gone()), "made")
    for aside in asides:
        damage(aside, how)
    kept = {aside.name: files_of(aside) for aside in asides}
    assert pipeline.stored(KEY) is None
    assert pipeline.read_stored(KEY, made.id, []) is None
    other = pipeline.lay_out(KEY, mode="tram")
    assert not made.dir.exists()
    assert {aside.name: files_of(aside) for aside in asides} == kept
    assert ids_under(KEY) == sorted([other.id, *kept])


@pytest.mark.parametrize("how", DAMAGES)
@pytest.mark.parametrize("platform", ["here", "windows"])
def test_a_whole_set_aside_survives_a_sweep_beside_a_damaged_place(home, monkeypatch, platform,
                                                                   how):
    """A whole set put aside, its writer gone, beside a place that holds a
    set that is not whole. The sweep removes a set aside only once its
    place holds a whole set again: here the aside may be all that is left
    of the layout, so it stays as it was, and so does the damaged place.
    Here the writer's process can be asked and is gone; on Windows it is
    presumed alive, which kept the aside before too."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    aside = made.dir.with_name(f"{made.id}.old-{gone()}")
    shutil.copytree(made.dir, aside)
    kept = files_of(aside)
    damage(made.dir, how)
    place = files_of(made.dir)
    if platform == "windows":
        as_windows(monkeypatch)
    other = pipeline.lay_out(KEY, mode="tram")
    assert files_of(aside) == kept, "the whole set aside is kept"
    assert files_of(made.dir) == place, "and the damaged place is not touched"
    assert ids_under(KEY) == sorted([made.id, other.id, aside.name])


def test_a_stored_meta_nested_past_the_parser_reads_as_not_stored(home, monkeypatch):
    """The set in its place, its meta nested deeper than the parser goes:
    ``read_layout``, ``stored`` and ``read_stored`` answer not stored, and
    the RecursionError goes no further."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    damage(made.dir, "a meta nested past the parser")
    assert pipeline.read_layout(KEY, made.id) is None
    assert pipeline.stored(KEY) is None
    assert pipeline.read_stored(KEY, made.id, []) is None


class Overflowing:
    """``datetime`` as a platform whose clock overflows at the year asked."""

    @staticmethod
    def fromisoformat(text: str):
        raise OverflowError("timestamp out of range for platform time_t")


@pytest.mark.parametrize("door", list(LOOKUPS))
@pytest.mark.parametrize("clock", ["a made in the year 1", "a clock that overflows"])
def test_a_made_no_timestamp_can_be_had_for_falls_back_to_the_folder(home, monkeypatch, clock,
                                                                     door):
    """Two whole sets left aside whose ``made`` gives no timestamp: the year
    1 without a zone, which this platform's clock refuses with a ValueError
    and another's with an OverflowError, which a stand-in clock raises here.
    Neither escapes a lookup: the sets are told apart by when their folders
    last changed, and the newer goes back."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    older, newer = two_aside(made, (1000, 200), "mtime")
    for aside in (older, newer):
        meta = json.loads((aside / pipeline.META_FILE).read_text())
        meta["made"] = "0001-01-01T00:00:00"
        (aside / pipeline.META_FILE).write_text(json.dumps(meta))
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))
    if clock == "a clock that overflows":
        monkeypatch.setattr(pipeline, "dt", types.SimpleNamespace(datetime=Overflowing))
    found = LOOKUPS[door](made.id)
    assert found is not None and found.id == made.id
    assert (made.dir / "which").read_text() == "newer"


@pytest.mark.parametrize("name", ["{id}.old-{pid}-backup", "{id}.old-{pid}.bak",
                                  "{id}.old-{pid}x", "{id}.older-{pid}"])
def test_a_folder_named_like_a_set_put_aside_but_not_one_is_left_alone(
        home, monkeypatch, name):
    """``_leftover`` reads a name whole: a person's own copy of a set named
    ``<id>.old-<pid>-backup`` is not a set a swap put aside. Its place empty,
    a lookup does not move it into it, and a sweep neither moves nor removes
    it, though its pid's process is gone."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    before = files_of(made.dir)
    theirs = made.dir.with_name(name.format(id=made.id, pid=gone()))
    made.dir.rename(theirs)
    assert pipeline.stored(KEY) is None
    other = pipeline.lay_out(KEY, mode="tram")
    assert ids_under(KEY) == sorted([other.id, theirs.name])
    assert files_of(theirs) == before


def test_read_layout_and_stored_layouts_never_put_a_set_back(home, monkeypatch):
    """The two reads that do not take the store's lock do not write either:
    a set left aside reads as not stored and is not listed, and stays where
    it is, until a door that takes the lock is asked for its layout."""
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    before = files_of(made.dir)
    aside = made.dir.with_name(f"{made.id}.old-{gone()}")
    made.dir.rename(aside)
    assert pipeline.read_layout(KEY, made.id) is None
    assert pipeline.stored_layouts(KEY) == []
    assert files_of(aside) == before and not made.dir.exists(), "neither read wrote"
    assert pipeline.read_stored(KEY, made.id, [])[0].id == made.id
    assert [layout.id for layout in pipeline.stored_layouts(KEY)] == [made.id]


# ---------------------------------------- feeds.remove under the store's lock

MINE = "mine"


def test_a_removal_deletes_the_layouts_only_once_a_forced_re_layouts_swap_is_done(
        tmp_path, monkeypatch):
    """A forced graph.build of a feed a person added is held between its
    swap's two renames, the stored set aside and the new one not yet in
    place, when a removal of the feed reaches the deletion of its layouts.
    The deletion waits for the store's lock and comes after the swap: the
    re-layout is never refused its second rename (which answered a kind
    ``io`` error naming a path), and once both are done nothing of the
    feed's layouts is left. The feed is forgotten before its layouts go, so
    graph.build answers that it is not registered, or, if the deletion came
    between the swap and its answer's read, that the layout was removed as
    it was read."""
    monkeypatch.setenv(config.ENV, str(tmp_path / "home"))
    monkeypatch.delenv(loom.COMMIT_ENV, raising=False)
    monkeypatch.delenv(loom.BIN_ENV, raising=False)
    source = tmp_path / "mine.zip"
    write_timetabled_feed(source)
    feeds.add(source, key=MINE)
    FakeLoom(monkeypatch)
    made = pipeline.lay_out(MINE)
    folder = config.graphs_dir() / MINE

    waited = threading.Event()
    monkeypatch.setattr(pipeline, "_lock", Watched(waited))
    in_gap, let_gap = threading.Event(), threading.Event()
    real_replace, real_rmtree = os.replace, shutil.rmtree
    deleted: list[list[str]] = []

    def replace(src, dst):
        real_replace(src, dst)
        if Path(src) == made.dir and Path(dst).name.startswith(f"{made.id}.old-"):
            in_gap.set()
            assert let_gap.wait(30), "the test never let the swap finish"

    def rmtree(path, *args, **kwargs):
        if Path(path) == folder:
            deleted.append(sorted(child.name for child in folder.iterdir()))
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(shutil, "rmtree", rmtree)
    client = Client()
    try:
        building = client.send("graph.build", {"key": MINE, "force": True})
        assert in_gap.wait(30), "the re-layout never reached its swap"
        removing = client.send("feeds.remove", {"key": MINE})
        deadline = time.monotonic() + 30
        while not (waited.is_set() or any(m.get("id") == removing for m in client.out)):
            assert time.monotonic() < deadline, "the removal neither waited nor answered"
            time.sleep(0.01)
        let_gap.set()
        removed, built = client.wait(removing, 30), client.wait(building, 30)
    finally:
        let_gap.set()
        client.endpoint.close()
    assert removed.get("result") == {"ok": True}, removed
    assert waited.is_set(), "the removal deleted the layouts without waiting for the store's lock"
    assert deleted == [[made.id]], "deleted once the swap was done, its set in place"
    assert "error" in built and built["error"]["data"]["kind"] in ("feed", "layout"), built
    assert not folder.exists(), "nothing of the feed's layouts is left"
    assert MINE not in feeds.all()
