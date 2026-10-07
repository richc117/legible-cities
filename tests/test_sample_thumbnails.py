"""The thumbnail pair of every sample city (issue 52).

``bin/thumbnails <folder>`` draws, for each preset with a stored layout, the
picture the desktop app's front door shows on that city's card. These tests
stand in ``pipeline.stored`` -- the worktree has no ``data/`` -- with real
``Layout`` records over a real ``octi`` stage file each, written from a
hand-made graph into the test's home, so the stage is read, checked and
projected as it is on a machine with layouts. What needs the real layouts (the
44 files, their total size) is measured on ``main``.
"""

import datetime as dt
import json
import re
import xml.etree.ElementTree as ET

import pytest
from test_feeds import gtfs_zip
from test_thumbnail import grid, three_lines

from schematic import config, feeds, loom, pipeline, sample_thumbnails, thumbnail
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph

DAY = dt.date(2026, 10, 7)
THEMES = ("dark", "light")
EMPTY = {"type": "FeatureCollection", "features": []}


def preset(key: str) -> feeds.Feed:
    return feeds.Feed(key=key, name=key.title(), url=f"https://example.invalid/{key}.zip")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv(config.ENV, str(tmp_path / "home"))
    monkeypatch.delenv(loom.COMMIT_ENV, raising=False)
    return tmp_path / "home"


@pytest.fixture
def registry(home, monkeypatch):
    """Three presets, ``alpha``, ``bravo`` and ``charlie``, in that order."""
    presets = {key: preset(key) for key in ("alpha", "bravo", "charlie")}
    monkeypatch.setattr(feeds, "FEEDS", presets)
    return presets


def lon_lat(graph: LineGraph) -> dict:
    """A graph as a stage file holds it: GeoJSON in degrees, which LOOM writes
    and the pipeline projects. The test graphs are in the hundreds, so they
    are scaled down to a corner of the map."""
    def scaled(coords):
        return [scaled(c) for c in coords] if isinstance(coords[0], list) else [
            c * 0.01 for c in coords]

    geojson = graph.to_geojson()
    for feature in geojson["features"]:
        feature["geometry"]["coordinates"] = scaled(feature["geometry"]["coordinates"])
    return geojson


def picture_of(graph: LineGraph) -> dict[str, str]:
    """What the pictures of a graph stored in a stage file must be: the stage,
    projected, drawn with nothing chosen."""
    return thumbnail.draw(LineGraph.from_geojson(lon_lat(graph)).reproject(to_mercator))


class Stubs:
    """``pipeline.stored`` over real layouts: a key in ``layouts`` has one,
    whose ``octi`` stage is that graph in degrees, and the rest have none. A
    key in ``empty`` has a stage with no edges, and one in ``unreadable`` a
    stage that cannot be read (a folder where the file should be, which names
    its own path in what it raises). ``pipeline.run`` is stood in with the
    sentence a schedule can raise, and every call to it is kept: a picture does
    not need a schedule, and a card must not lose its picture to one."""

    def __init__(self, monkeypatch, home, layouts, *, empty=(), unreadable=()):
        self.asked: list[str] = []
        self.runs: list[str] = []
        self.layouts: dict[str, pipeline.Layout] = {}
        for key, graph in layouts.items():
            folder = home / "stages" / key
            layout = pipeline.Layout(key=key, id=f"layout-{key}", dir=folder, meta={})
            stage = layout.paths["octi"]
            stage.parent.mkdir(parents=True)
            if key in unreadable:
                stage.mkdir()
            else:
                stage.write_text(json.dumps(EMPTY if key in empty else lon_lat(graph)),
                                 encoding="utf-8")
            self.layouts[key] = layout
        monkeypatch.setattr(pipeline, "stored", self.stored)
        monkeypatch.setattr(pipeline, "run", self.run)

    def stored(self, key, **overrides):
        self.asked.append(key)
        return self.layouts.get(key)

    def run(self, key, **rest):
        self.runs.append(key)
        raise ValueError("feed has neither calendar.txt nor calendar_dates.txt; nothing can "
                         "be scheduled")


def two_of_three(monkeypatch, home):
    return Stubs(monkeypatch, home, {"alpha": three_lines(), "bravo": grid(3)})


# ------------------------------------------------------------ what is written

def test_two_presets_with_layouts_give_four_files_and_a_readme_naming_the_third(
        registry, home, monkeypatch, tmp_path):
    two_of_three(monkeypatch, home)
    out = tmp_path / "samples"
    report = sample_thumbnails.write(out, today=DAY)

    assert sorted(p.name for p in out.iterdir()) == [
        "README.md", "alpha-dark.svg", "alpha-light.svg", "bravo-dark.svg", "bravo-light.svg"]
    assert [p.name for p in report.written] == [
        "alpha-dark.svg", "alpha-light.svg", "bravo-dark.svg", "bravo-light.svg"]
    assert report.missing == {"charlie": "not downloaded"}
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert "- `charlie`: not downloaded" in readme
    assert "- Files: 4" in readme
    # No path of the machine it was made on goes into a file that is shipped.
    assert str(tmp_path) not in readme
    # Each file is the picture of its own city's stage in its own palette, in
    # the feed's colours and the engine's order: nothing chosen.
    for key, graph in (("alpha", three_lines()), ("bravo", grid(3))):
        drawn = picture_of(graph)
        for theme in THEMES:
            assert (out / f"{key}-{theme}.svg").read_text(encoding="utf-8") == drawn[theme]
    assert (out / "alpha-dark.svg").read_text() != (out / "bravo-dark.svg").read_text()
    assert (out / "alpha-dark.svg").read_text() != (out / "alpha-light.svg").read_text()


def test_a_preset_that_is_downloaded_but_not_laid_out_says_so(
        registry, home, monkeypatch, tmp_path):
    two_of_three(monkeypatch, home)
    zip_path = feeds.get("charlie").zip_path
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(b"a feed")
    report = sample_thumbnails.write(tmp_path / "samples", today=DAY)

    assert report.missing == {"charlie": "no layout at this LOOM"}
    assert "- `charlie`: no layout at this LOOM" in (
        tmp_path / "samples" / "README.md").read_text(encoding="utf-8")


def test_the_files_are_named_by_city_and_palette_and_not_as_a_projects_are(
        registry, home, monkeypatch, tmp_path):
    two_of_three(monkeypatch, home)
    report = sample_thumbnails.write(tmp_path / "samples", today=DAY)

    names = {p.name for p in report.written}
    assert names == {f"{key}-{theme}.svg" for key in ("alpha", "bravo") for theme in THEMES}
    assert not any("thumb" in name for name in names)
    # The names a project's pair has are another thing, beside its page.
    assert names.isdisjoint(thumbnail.files_for("alpha").values())


@pytest.mark.parametrize("theme", THEMES)
def test_no_file_has_a_variable_or_a_text_and_each_is_an_svg(
        theme, registry, home, monkeypatch, tmp_path):
    two_of_three(monkeypatch, home)
    report = sample_thumbnails.write(tmp_path / "samples", today=DAY)

    pictures = [p for p in report.written if p.name.endswith(f"-{theme}.svg")]
    assert len(pictures) == 2
    for path in pictures:
        svg = path.read_text(encoding="utf-8")
        assert "var(" not in svg
        assert "<text" not in svg
        assert ET.fromstring(svg).tag == "{http://www.w3.org/2000/svg}svg"


# ---------------------------------------------------------------- what is said

def test_the_total_size_and_the_missing_keys_are_printed_and_the_size_is_the_files(
        registry, home, monkeypatch, tmp_path, capsys):
    two_of_three(monkeypatch, home)
    out = tmp_path / "samples"
    assert sample_thumbnails.main([str(out)]) == 0

    said = capsys.readouterr().out
    count, size = re.search(r"^(\d+) files, (\d+) bytes$", said, re.M).groups()
    on_disk = [p for p in out.iterdir() if p.suffix == ".svg"]
    assert int(count) == len(on_disk) == 4
    # The pictures, to the byte: the README, which carries the number, is not
    # one of the files it counts.
    assert int(size) == sum(p.stat().st_size for p in on_disk) > 0
    assert f"Total size: {size} bytes" in (out / "README.md").read_text(encoding="utf-8")
    assert "no picture: charlie (not downloaded)" in said


def test_the_readme_records_the_engine_the_loom_and_the_date(
        registry, home, monkeypatch, tmp_path):
    Stubs(monkeypatch, home, {key: three_lines() for key in registry})
    monkeypatch.setenv(loom.COMMIT_ENV, "0123abcd")
    out = tmp_path / "samples"
    sample_thumbnails.write(out, today=DAY)

    readme = (out / "README.md").read_text(encoding="utf-8")
    assert f"- Engine: {sample_thumbnails.__version__}" in readme
    assert "- LOOM commit: 0123abcd" in readme
    assert "- Made on: 2026-10-07" in readme
    assert "- Files: 6" in readme
    # Nothing is missing, and the README says so rather than list nothing.
    assert "every preset has one" in readme and "- `" not in readme


def test_a_loom_the_host_did_not_name_is_said_so(registry, home, monkeypatch, tmp_path):
    two_of_three(monkeypatch, home)
    sample_thumbnails.write(tmp_path / "samples", today=DAY)

    assert "- LOOM commit: not recorded" in (
        tmp_path / "samples" / "README.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------- stability

def test_running_twice_writes_the_same_bytes_in_every_picture(
        registry, home, monkeypatch, tmp_path):
    two_of_three(monkeypatch, home)
    # On two days: the date is the README's and no SVG may carry it.
    sample_thumbnails.write(tmp_path / "first", today=DAY)
    sample_thumbnails.write(tmp_path / "second", today=DAY + dt.timedelta(days=30))

    pictures = sorted(p.name for p in (tmp_path / "first").glob("*.svg"))
    assert len(pictures) == 4
    for name in pictures:
        assert (tmp_path / "first" / name).read_bytes() == (tmp_path / "second" / name).read_bytes()
    assert (tmp_path / "first" / "README.md").read_bytes() != (
        tmp_path / "second" / "README.md").read_bytes()


# ----------------------------------------------------------- which cities

def test_a_feed_a_person_added_is_not_a_sample_city(registry, home, monkeypatch, tmp_path):
    mine = feeds.add(gtfs_zip(tmp_path / "mine.zip"), key="mine", name="Mine")
    assert set(feeds.all()) == {*registry, mine.key}
    # Every key, the person's included, has a layout to draw from.
    stubs = Stubs(monkeypatch, home, {key: three_lines() for key in feeds.all()})
    out = tmp_path / "samples"
    report = sample_thumbnails.write(out, today=DAY)

    assert "mine" not in stubs.asked
    assert not list(out.glob("mine-*"))
    assert "mine" not in report.missing
    assert "mine" not in (out / "README.md").read_text(encoding="utf-8")
    assert len(report.written) == 6


# ---------------------------------------------------------- how it is drawn

def test_a_picture_is_read_from_the_stored_stage_and_no_schedule_is_made_for_it(
        registry, home, monkeypatch, tmp_path):
    stubs = two_of_three(monkeypatch, home)
    out = tmp_path / "samples"
    report = sample_thumbnails.write(out, today=DAY)

    # Every preset is asked of the store, in the registry's order, and the
    # ones that have a layout are drawn from it without a draw of the map:
    # ``run``'s schedule would refuse a feed whose picture draws fine.
    assert stubs.asked == ["alpha", "bravo", "charlie"]
    assert stubs.runs == []
    assert len(report.written) == 4 and not report.failed
    # Nothing else is made: no page, and nothing in the engine's out/.
    assert sorted(p.name for p in out.iterdir() if p.suffix != ".svg") == ["README.md"]
    assert not config.out_dir().exists()


def test_a_layout_that_will_not_draw_is_named_and_the_run_fails_but_goes_on(
        registry, home, monkeypatch, tmp_path, capsys):
    Stubs(monkeypatch, home,
          {"alpha": three_lines(), "bravo": grid(3), "charlie": three_lines()},
          empty=["bravo"])
    out = tmp_path / "samples"
    out.mkdir()
    # An earlier run drew bravo, when it had edges.
    for theme in THEMES:
        (out / f"bravo-{theme}.svg").write_text("<svg/>", encoding="utf-8")
    with pytest.raises(ValueError) as refused:
        pipeline.require_edges(registry["bravo"], LineGraph.from_geojson(EMPTY))
    sentence = " ".join(str(refused.value).split())
    assert "the line graph is empty" in sentence

    status = sample_thumbnails.main([str(out)])

    # The others are drawn; the one is named with the engine's sentence, on
    # one line; and the exit status says the run did not all work.
    assert status == 1
    assert sorted(p.name for p in out.glob("*.svg")) == [
        "alpha-dark.svg", "alpha-light.svg", "charlie-dark.svg", "charlie-light.svg"]
    why = f"could not be drawn: {sentence}"
    readme = (out / "README.md").read_text(encoding="utf-8")
    assert f"- `bravo`: {why}" in readme
    assert str(tmp_path) not in readme
    assert f"no picture: bravo ({why})" in capsys.readouterr().out


def test_a_stage_that_cannot_be_read_is_not_caught_and_no_path_reaches_a_file(
        registry, home, monkeypatch, tmp_path, capsys):
    Stubs(monkeypatch, home, {"alpha": three_lines(), "bravo": grid(3)}, unreadable=["bravo"])
    out = tmp_path / "samples"

    # An OSError names the file it could not read, and a README is committed
    # elsewhere: it stops the run, whole, and says so on stderr.
    with pytest.raises(OSError) as refused:
        sample_thumbnails.write(out, today=DAY)
    assert str(home) in str(refused.value)
    assert not (out / "README.md").exists()
    assert sample_thumbnails.main([str(out)]) == 1
    assert "bin/thumbnails:" in capsys.readouterr().err
    assert not (out / "README.md").exists()
    for path in out.iterdir():
        assert str(home) not in path.read_text(encoding="utf-8")


# ------------------------------------------------------------- the folder

def test_a_picture_an_earlier_run_left_for_a_preset_with_none_is_removed(
        registry, home, monkeypatch, tmp_path):
    two_of_three(monkeypatch, home)
    out = tmp_path / "samples"
    out.mkdir()
    for name in ("charlie-dark.svg", "charlie-light.svg", "alpha-dark.svg",
                 "zulu-dark.svg", "notes.txt"):
        (out / name).write_text("left from before", encoding="utf-8")
    sample_thumbnails.write(out, today=DAY)

    # The pair of the preset with none is gone, and the README says it has none.
    assert not (out / "charlie-dark.svg").exists() and not (out / "charlie-light.svg").exists()
    assert "- `charlie`: not downloaded" in (out / "README.md").read_text(encoding="utf-8")
    # What a preset with a layout has is written over, and what is no
    # preset's is not touched.
    assert (out / "alpha-dark.svg").read_text(encoding="utf-8") == picture_of(three_lines())["dark"]
    assert (out / "zulu-dark.svg").read_text(encoding="utf-8") == "left from before"
    assert (out / "notes.txt").read_text(encoding="utf-8") == "left from before"


def test_a_machine_with_nothing_downloaded_writes_a_readme_and_no_pictures(
        registry, home, monkeypatch, tmp_path, capsys):
    Stubs(monkeypatch, home, {})
    out = tmp_path / "samples"

    assert sample_thumbnails.main([str(out)]) == 0
    assert [p.name for p in out.iterdir()] == ["README.md"]
    said = capsys.readouterr().out
    assert said.startswith("0 files, 0 bytes\n")
    assert said.count("no picture:") == 3
