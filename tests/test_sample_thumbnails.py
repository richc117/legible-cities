"""The thumbnail pair of every sample city (issue 52).

``bin/thumbnails <folder>`` draws, for each preset with a stored layout, the
picture the desktop app's front door shows on that city's card. These tests
stub what the worktree has no ``data/`` for -- ``pipeline.stored`` and
``pipeline.run`` -- with hand-made graphs, so they run anywhere. What needs
the real layouts (the 44 files, their total size) is measured on ``main``.
"""

import datetime as dt
import os
import re
import xml.etree.ElementTree as ET
from types import SimpleNamespace

import pytest
from test_feeds import gtfs_zip
from test_thumbnail import grid, three_lines

from schematic import config, feeds, loom, pipeline, sample_thumbnails, thumbnail

DAY = dt.date(2026, 10, 7)
THEMES = ("dark", "light")


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


class Stubs:
    """``pipeline.stored`` and ``pipeline.run`` over hand-made graphs: a key
    in ``layouts`` has a stored layout and draws that graph, and the rest have
    none. Every call is kept, and ``run`` writes a by-product beside where it
    is told, as the real one writes a page."""

    def __init__(self, monkeypatch, layouts, *, cannot_draw=()):
        self.layouts = layouts
        self.cannot_draw = set(cannot_draw)
        self.asked: list[str] = []
        self.runs: list[tuple[str, str | None, object]] = []
        monkeypatch.setattr(pipeline, "stored", self.stored)
        monkeypatch.setattr(pipeline, "run", self.run)

    def stored(self, key, **overrides):
        self.asked.append(key)
        return SimpleNamespace(id=f"layout-{key}") if key in self.layouts else None

    def run(self, key, *, layout=None, out_dir=None, **rest):
        self.runs.append((key, layout, out_dir))
        if layout is None:
            raise AssertionError(f"{key} was laid out, not read from the store")
        if key in self.cannot_draw:
            raise ValueError(f"{key}: the line graph is empty\nmatched no routes")
        folder = out_dir or config.out_dir()
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{key}.html").write_text("a page", encoding="utf-8")
        return SimpleNamespace(graph=self.layouts[key])


def two_of_three(monkeypatch):
    return Stubs(monkeypatch, {"alpha": three_lines(), "bravo": grid(3)})


# ------------------------------------------------------------ what is written

def test_two_presets_with_layouts_give_four_files_and_a_readme_naming_the_third(
        registry, monkeypatch, tmp_path):
    two_of_three(monkeypatch)
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
    # Each file is the picture of its own city's graph in its own palette, in
    # the feed's colours and the engine's order: nothing chosen.
    for key, graph in (("alpha", three_lines()), ("bravo", grid(3))):
        drawn = thumbnail.draw(graph)
        for theme in THEMES:
            assert (out / f"{key}-{theme}.svg").read_text(encoding="utf-8") == drawn[theme]
    assert (out / "alpha-dark.svg").read_text() != (out / "bravo-dark.svg").read_text()
    assert (out / "alpha-dark.svg").read_text() != (out / "alpha-light.svg").read_text()


def test_a_preset_that_is_downloaded_but_not_laid_out_says_so(registry, monkeypatch, tmp_path):
    two_of_three(monkeypatch)
    zip_path = feeds.get("charlie").zip_path
    zip_path.parent.mkdir(parents=True)
    zip_path.write_bytes(b"a feed")
    report = sample_thumbnails.write(tmp_path / "samples", today=DAY)

    assert report.missing == {"charlie": "no layout at this LOOM"}
    assert "- `charlie`: no layout at this LOOM" in (
        tmp_path / "samples" / "README.md").read_text(encoding="utf-8")


def test_the_files_are_named_by_city_and_palette_and_not_as_a_projects_are(
        registry, monkeypatch, tmp_path):
    two_of_three(monkeypatch)
    report = sample_thumbnails.write(tmp_path / "samples", today=DAY)

    names = {p.name for p in report.written}
    assert names == {f"{key}-{theme}.svg" for key in ("alpha", "bravo") for theme in THEMES}
    assert not any("thumb" in name for name in names)
    # The names a project's pair has are another thing, beside its page.
    assert names.isdisjoint(thumbnail.files_for("alpha").values())


@pytest.mark.parametrize("theme", THEMES)
def test_no_file_has_a_variable_or_a_text_and_each_is_an_svg(
        theme, registry, monkeypatch, tmp_path):
    two_of_three(monkeypatch)
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
        registry, monkeypatch, tmp_path, capsys):
    two_of_three(monkeypatch)
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


def test_the_readme_records_the_engine_the_loom_and_the_date(registry, monkeypatch, tmp_path):
    Stubs(monkeypatch, {key: three_lines() for key in registry})
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


def test_a_loom_the_host_did_not_name_is_said_so(registry, monkeypatch, tmp_path):
    two_of_three(monkeypatch)
    sample_thumbnails.write(tmp_path / "samples", today=DAY)

    assert "- LOOM commit: not recorded" in (
        tmp_path / "samples" / "README.md").read_text(encoding="utf-8")


# ----------------------------------------------------------------- stability

def test_running_twice_writes_the_same_bytes_in_every_picture(registry, monkeypatch, tmp_path):
    two_of_three(monkeypatch)
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

def test_a_feed_a_person_added_is_not_a_sample_city(registry, monkeypatch, tmp_path):
    mine = feeds.add(gtfs_zip(tmp_path / "mine.zip"), key="mine", name="Mine")
    assert set(feeds.all()) == {*registry, mine.key}
    # Every key, the person's included, has a layout to draw from.
    stubs = Stubs(monkeypatch, {key: three_lines() for key in feeds.all()})
    out = tmp_path / "samples"
    report = sample_thumbnails.write(out, today=DAY)

    assert "mine" not in stubs.asked and not [r for r in stubs.runs if r[0] == "mine"]
    assert not list(out.glob("mine-*"))
    assert "mine" not in report.missing
    assert "mine" not in (out / "README.md").read_text(encoding="utf-8")
    assert len(report.written) == 6


# ---------------------------------------------------------- how it is drawn

def test_a_picture_is_read_from_the_stored_layout_and_its_page_is_thrown_away(
        registry, monkeypatch, tmp_path):
    stubs = two_of_three(monkeypatch)
    sample_thumbnails.write(tmp_path / "samples", today=DAY)

    # Laid out never: every draw names the layout the store answered. A key
    # with none is not drawn at all.
    assert [(key, layout) for key, layout, _ in stubs.runs] == [
        ("alpha", "layout-alpha"), ("bravo", "layout-bravo")]
    # The page the draw writes goes to a folder of its own and not to the
    # engine's out/, and the folder is gone with the run.
    folders = [folder for _, _, folder in stubs.runs]
    assert all(folder is not None for folder in folders)
    assert not config.out_dir().exists()
    assert not any(folder.exists() for folder in folders)
    assert (tmp_path / "samples").is_dir()
    assert not list((tmp_path / "samples").glob("*.html"))


def test_a_layout_that_will_not_draw_is_named_and_the_run_fails_but_goes_on(
        registry, monkeypatch, tmp_path, capsys):
    Stubs(monkeypatch, {"alpha": three_lines(), "bravo": grid(3), "charlie": three_lines()},
          cannot_draw=["bravo"])
    out = tmp_path / "samples"
    status = sample_thumbnails.main([str(out)])

    # The others are drawn; the one is named with the engine's sentence, on
    # one line; and the exit status says the run did not all work.
    assert status == 1
    assert sorted(p.name for p in out.glob("*.svg")) == [
        "alpha-dark.svg", "alpha-light.svg", "charlie-dark.svg", "charlie-light.svg"]
    why = "could not be drawn: bravo: the line graph is empty matched no routes"
    assert f"- `bravo`: {why}" in (out / "README.md").read_text(encoding="utf-8")
    assert f"no picture: bravo ({why})" in capsys.readouterr().out


def test_a_machine_with_nothing_downloaded_writes_a_readme_and_no_pictures(
        registry, monkeypatch, tmp_path, capsys):
    Stubs(monkeypatch, {})
    out = tmp_path / "samples"

    assert sample_thumbnails.main([str(out)]) == 0
    assert os.listdir(out) == ["README.md"]
    said = capsys.readouterr().out
    assert said.startswith("0 files, 0 bytes\n")
    assert said.count("no picture:") == 3
