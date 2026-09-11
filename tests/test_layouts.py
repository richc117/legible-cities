"""schematic.pipeline's layouts: named by their inputs, written whole or not
at all, never rewritten in place, and migrated once from the flat set.

LOOM is a stand-in here -- ``loom.gtfs2graph`` and ``loom.run`` are replaced
with functions that return small graphs and record what they were asked --
so every rule about the directory is asserted in milliseconds, without
Docker, binaries or a feed. What needs the real thing is in test_serve.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import zipfile
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from schematic import config, feeds, loom, pipeline, serve

KEY = "la-metro-rail"

GRAPH = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"id": "n1", "station_id": "a", "station_label": "A"},
     "geometry": {"type": "Point", "coordinates": [-118.2, 34.0]}},
    {"type": "Feature", "properties": {"id": "n2", "station_id": "b", "station_label": "B"},
     "geometry": {"type": "Point", "coordinates": [-118.3, 34.1]}},
    {"type": "Feature", "properties": {"from": "n1", "to": "n2",
                                       "lines": [{"id": "l1", "label": "A", "color": "ff0000"}]},
     "geometry": {"type": "LineString", "coordinates": [[-118.2, 34.0], [-118.3, 34.1]]}},
]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch home with a small feed zip in place of the download."""
    monkeypatch.setenv(config.ENV, str(tmp_path))
    monkeypatch.delenv(loom.COMMIT_ENV, raising=False)
    monkeypatch.delenv(loom.BIN_ENV, raising=False)
    feeds_dir = tmp_path / "data" / "feeds"
    feeds_dir.mkdir(parents=True)
    write_feed(feeds_dir / f"{KEY}.zip", "1")
    return tmp_path


def write_feed(path: Path, marker: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("agency.txt", "agency_id,agency_name\nLACMTA,Metro\n")
        zf.writestr("routes.txt",
                    f"route_id,agency_id,route_short_name,route_long_name,route_type\n"
                    f"r{marker},LACMTA,A,Metro A Line,0\n")
        zf.writestr("trips.txt", "route_id,service_id,trip_id\nr1,s1,t1\n")
        zf.writestr("stops.txt", "stop_id,stop_name,stop_lat,stop_lon\na,A,34.0,-118.2\n")


class FakeLoom:
    """What the stand-in tools were asked, in order; ``fail_at`` raises there."""

    def __init__(self, monkeypatch, *, fail_at: str | None = None, on_tool=None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at
        self.on_tool = on_tool
        monkeypatch.setattr(loom, "gtfs2graph", self.gtfs2graph)
        monkeypatch.setattr(loom, "run", self.run)

    def _tool(self, tool: str, *args: str) -> dict:
        self.calls.append(" ".join([tool, *args]))
        if self.on_tool is not None:
            self.on_tool(tool)
        if tool == self.fail_at:
            raise loom.Cancelled(f"{tool}: cancelled")
        graph = json.loads(json.dumps(GRAPH))
        graph["stage"] = tool
        return graph

    def gtfs2graph(self, feed_zip, *args, timeout=None):
        assert Path(feed_zip).is_file(), "gtfs2graph is handed the normalised zip"
        return self._tool("gtfs2graph", *args)

    def run(self, tool, graph, *args, timeout=None):
        assert isinstance(graph, bytes), "a stage gets the previous stage's bytes"
        return self._tool(tool, *args)


def ids_under(key: str) -> list[str]:
    folder = config.graphs_dir() / key
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# ------------------------------------------------------------------ naming

def test_the_id_is_known_before_anything_runs_and_follows_every_input(home, monkeypatch):
    feed = feeds.FEEDS[KEY]
    digest = pipeline.feed_digest(feed.zip_path)
    base = pipeline.layout_id(pipeline.layout_inputs(feed, feed_sha256=digest, loom_commit=None))
    assert pipeline.stored(KEY) is None, "nothing is stored yet"
    fake = FakeLoom(monkeypatch)
    made = pipeline.lay_out(KEY)
    assert made.id == base
    assert fake.calls == [f"gtfs2graph -m {feed.mode}", "topo", "loom", "octi"]

    # Every input moves the id; nothing else does.
    def id_for(**over):
        f = feeds.resolved(KEY, **over)
        d = pipeline.feed_digest(feeds.FEEDS[KEY].zip_path)
        return pipeline.layout_id(pipeline.layout_inputs(f, feed_sha256=d, loom_commit=None))

    assert id_for() == base
    seen = {base, id_for(mode="tram"), id_for(agency="LACMTA"),
            id_for(label_pattern="^Metro (.+) Line$"), id_for(label_strip="-N$")}
    assert len(seen) == 5
    assert pipeline.layout_id(pipeline.layout_inputs(
        feed, feed_sha256=digest, loom_commit="abc")) != base
    write_feed(feed.zip_path, "2")
    assert id_for() != base, "different feed bytes, different layout"


def test_a_stored_layout_is_answered_without_running_anything(home, monkeypatch):
    fake = FakeLoom(monkeypatch)
    first = pipeline.lay_out(KEY)
    assert sorted(p.name for p in first.dir.iterdir()) == [
        ".meta.json", "00_gtfs2graph.json", "01_topo.json", "02_loom.json", "03_octi.json"]
    meta = json.loads((first.dir / ".meta.json").read_text())
    assert meta["feed"] == KEY and meta["migrated"] is False and meta["loom"] is None
    assert meta["engine"] and meta["made"].endswith("+00:00")
    assert meta["stages"][0] == ["gtfs2graph", ["-m", "all"]]
    # The meta is a type the app generates from the schema.
    schema = serve.schema()
    jsonschema.validate(meta, {"$ref": "#/$defs/LayoutMeta", "$defs": schema["$defs"]})

    fake.calls.clear()
    reports: list[str] = []
    again = pipeline.lay_out(KEY, progress=lambda stage, _f, _m: reports.append(stage))
    assert again.id == first.id and again.dir == first.dir
    assert fake.calls == [], "a stored layout runs nothing"
    assert reports == ["gtfs2graph", "topo", "loom", "octi"], "and is still reported stage by stage"
    assert pipeline.stored(KEY).id == first.id
    assert pipeline.schematize(KEY) == first.paths


def test_different_options_get_a_layout_beside_the_first_which_is_untouched(home, monkeypatch):
    FakeLoom(monkeypatch)
    first = pipeline.lay_out(KEY)
    stamp = {p.name: p.stat().st_mtime_ns for p in first.dir.iterdir()}
    second = pipeline.lay_out(KEY, mode="tram")
    assert second.id != first.id
    assert ids_under(KEY) == sorted([first.id, second.id])
    assert {p.name: p.stat().st_mtime_ns for p in first.dir.iterdir()} == stamp
    assert second.meta["mode"] == "tram" and second.feed.mode == "tram"
    # The overridden feed had a normalised copy of its own, so the registry's
    # copy was not rewritten with its options.
    copies = sorted(p.name for p in (home / "data" / "feeds").glob("*.normalized.zip"))
    assert len(copies) == 2 and f"{KEY}.normalized.zip" in copies
    assert pipeline.stored(KEY, mode="tram").id == second.id
    assert pipeline.stored(KEY).id == first.id


# ------------------------------------------------------- whole or nothing

def test_force_keeps_the_stored_layout_until_the_new_set_is_whole(home, monkeypatch):
    FakeLoom(monkeypatch)
    first = pipeline.lay_out(KEY)
    old_bytes = {p.name: p.read_bytes() for p in first.dir.iterdir()}
    seen_during: dict[str, object] = {}

    def watch(tool: str) -> None:
        if tool == "octi":
            # While the last stage runs, the stored layout is still whole and
            # the target directory is still the old one.
            seen_during["stored"] = pipeline.read_layout(KEY, first.id) is not None
            seen_during["old_bytes"] = {p.name: p.read_bytes() for p in first.dir.iterdir()}
            seen_during["scratch"] = sorted(p.name for p in (config.graphs_dir() / KEY).iterdir())

    FakeLoom(monkeypatch, on_tool=watch)
    (first.dir / "03_octi.json").write_text('{"marker": "old"}')
    old_bytes["03_octi.json"] = b'{"marker": "old"}'
    forced = pipeline.lay_out(KEY, force=True)
    assert forced.id == first.id
    assert seen_during["stored"] is True
    assert seen_during["old_bytes"] == old_bytes
    assert any(".building-" in name for name in seen_during["scratch"])
    assert (first.dir / "03_octi.json").read_text() != '{"marker": "old"}', "replaced once whole"
    assert ids_under(KEY) == [first.id], "no scratch and nothing put aside is left"


def test_a_cancel_during_octi_leaves_the_stored_layout_and_no_scratch(home, monkeypatch):
    FakeLoom(monkeypatch)
    first = pipeline.lay_out(KEY)
    old_bytes = {p.name: p.read_bytes() for p in first.dir.iterdir()}
    FakeLoom(monkeypatch, fail_at="octi")
    with pytest.raises(loom.Cancelled):
        pipeline.lay_out(KEY, force=True)
    assert ids_under(KEY) == [first.id]
    assert {p.name: p.read_bytes() for p in first.dir.iterdir()} == old_bytes
    # And a first build that is cancelled stores nothing at all.
    shutil.rmtree(first.dir)
    with pytest.raises(loom.Cancelled):
        pipeline.lay_out(KEY)
    assert ids_under(KEY) == []
    assert pipeline.stored(KEY) is None


def test_what_a_crash_leaves_is_swept_and_a_set_put_aside_comes_back(home, monkeypatch):
    FakeLoom(monkeypatch)
    first = pipeline.lay_out(KEY)
    folder = config.graphs_dir() / KEY
    (folder / f"{first.id}.building-999999-x").mkdir()
    (folder / f"{first.id}.building-999999-x" / "00_gtfs2graph.json").write_text("{}")
    # Interrupted between the two renames: the layout sits aside, the target is gone.
    aside = folder / f"{first.id}.old-999999"
    first.dir.rename(aside)
    assert pipeline.read_layout(KEY, first.id) is None
    pipeline.lay_out(KEY, force=True)
    assert ids_under(KEY) == [first.id]


# ---------------------------------------------------------------- migration

def test_a_flat_set_from_before_is_migrated_once_in_place(home, monkeypatch):
    folder = config.graphs_dir() / KEY
    folder.mkdir(parents=True)
    flat = {}
    for name in pipeline.STAGE_FILES.values():
        (folder / name).write_text(json.dumps({**GRAPH, "flat": name}))
        flat[name] = (folder / name).read_bytes()
    fake = FakeLoom(monkeypatch)

    found = pipeline.stored(KEY)
    assert found is not None
    assert found.meta["migrated"] is True
    assert found.meta["feed_sha256"] == pipeline.feed_digest(feeds.FEEDS[KEY].zip_path)
    assert {p.name: p.read_bytes() for p in found.dir.iterdir() if p.name != ".meta.json"} == flat
    assert not any((folder / name).exists() for name in flat), "moved, not copied"
    assert ids_under(KEY) == [found.id]
    assert fake.calls == []
    # Laying out finds the migrated set; nothing runs.
    assert pipeline.lay_out(KEY).id == found.id
    assert fake.calls == []


def test_a_flat_set_is_adopted_only_by_the_registrys_own_layout(home, monkeypatch):
    """A flat set was built from the registry entry and nothing else, so a
    request with an override must not claim it."""
    folder = config.graphs_dir() / KEY
    folder.mkdir(parents=True)
    for name in pipeline.STAGE_FILES.values():
        (folder / name).write_text(json.dumps(GRAPH))
    fake = FakeLoom(monkeypatch)
    assert pipeline.stored(KEY, mode="tram") is None
    assert all((folder / name).exists() for name in pipeline.STAGE_FILES.values())
    variant = pipeline.lay_out(KEY, mode="tram")
    assert variant.meta["migrated"] is False and fake.calls, "built, not adopted"
    assert pipeline.stored(KEY).meta["migrated"] is True, "the registry's layout adopts it"


def test_a_layout_remembers_a_recorded_null_over_the_registry(home, monkeypatch):
    FakeLoom(monkeypatch)
    first = pipeline.lay_out(KEY)
    assert first.meta["agency"] is None
    monkeypatch.setitem(feeds.FEEDS, KEY, replace(feeds.FEEDS[KEY], agency="LACMTA"))
    assert first.feed.agency is None, "the layout applied no agency, whatever the registry says now"
    assert pipeline.stored(KEY) is None, "and the registry's new layout is another one"


def test_two_builds_of_one_layout_at_once_make_one_layout(home, monkeypatch):
    """Two projects on one feed press Lay out together: the second waits for
    the first and reads what it stored; nothing is built twice, nothing is
    swept from under a live build, and no scratch is left."""
    layouts: list[pipeline.Layout] = []
    threads: list[threading.Thread] = []

    def second() -> None:
        layouts.append(pipeline.lay_out(KEY))

    def slow_first(tool: str) -> None:
        if tool == "topo" and not threads:
            thread = threading.Thread(target=second)
            thread.start()
            threads.append(thread)
            # The second caller is waiting on the first, not building beside it.
            thread.join(timeout=0.2)
            assert thread.is_alive(), "the second build should be waiting, not building"

    fake = FakeLoom(monkeypatch, on_tool=slow_first)
    first = pipeline.lay_out(KEY)
    threads[0].join(timeout=10)
    assert [l.id for l in layouts] == [first.id]
    assert fake.calls.count("topo") == 1, "one build, not two"
    assert ids_under(KEY) == [first.id]


def test_a_sweep_leaves_a_live_builds_scratch_alone(home, monkeypatch):
    FakeLoom(monkeypatch)
    folder = config.graphs_dir() / KEY
    folder.mkdir(parents=True)
    mine = folder / f"{'a' * 64}.building-{os.getpid()}-x"
    mine.mkdir()
    dead = folder / f"{'b' * 64}.building-999999-x"
    dead.mkdir()
    pipeline.lay_out(KEY)
    assert mine.is_dir(), "this process's scratch is presumed live"
    assert not dead.exists(), "a dead process's scratch goes"


def test_an_incomplete_flat_set_is_left_alone(home):
    folder = config.graphs_dir() / KEY
    folder.mkdir(parents=True)
    (folder / "03_octi.json").write_text("{}")
    assert pipeline.stored(KEY) is None
    assert (folder / "03_octi.json").exists()


# ------------------------------------------------------------- drawing from

def test_run_refuses_a_layout_that_is_not_stored(home, monkeypatch):
    FakeLoom(monkeypatch)
    with pytest.raises(pipeline.LayoutMissing, match="lay the feed out first"):
        pipeline.run(KEY, layout="0" * 64)


def test_stage_path_and_stored_never_download_or_build(home, monkeypatch):
    fake = FakeLoom(monkeypatch)
    assert pipeline.stage_path(KEY, "octi") is None
    feeds.FEEDS[KEY].zip_path.unlink()
    assert pipeline.stored(KEY) is None
    assert fake.calls == []
