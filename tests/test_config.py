"""The engine's home: where feeds, graphs and output go.

Unset, everything lands beside ``src/`` as it always has. Set, nothing the
engine writes lands anywhere else -- an installed application cannot write
beside its own code, and this is the one variable it has to set.
"""

import io
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from schematic import config, export, feeds, pipeline, site


@pytest.fixture
def unset(monkeypatch):
    monkeypatch.delenv(config.ENV, raising=False)


@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV, str(tmp_path))
    return tmp_path


def test_repo_root_is_the_checkout():
    assert (config.REPO_ROOT / "src" / "schematic" / "config.py").is_file()
    assert (config.REPO_ROOT / "pyproject.toml").is_file()


def test_unset_means_the_repository_root(unset):
    assert config.home() == config.REPO_ROOT
    assert config.feeds_dir() == config.REPO_ROOT / "data" / "feeds"
    assert config.graphs_dir() == config.REPO_ROOT / "data" / "graphs"
    assert config.out_dir() == config.REPO_ROOT / "out"


def test_empty_counts_as_unset(monkeypatch):
    monkeypatch.setenv(config.ENV, "")
    assert config.home() == config.REPO_ROOT


def test_set_moves_every_data_path(home):
    assert config.home() == home
    assert config.feeds_dir() == home / "data" / "feeds"
    assert config.graphs_dir() == home / "data" / "graphs"
    assert config.out_dir() == home / "out"


def test_tilde_and_relative_values_are_made_absolute(monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV, "~/somewhere")
    assert config.home() == Path.home() / "somewhere"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(config.ENV, "here")
    assert config.home() == tmp_path / "here"


def test_feed_cache_follows_the_home(home):
    feed = feeds.FEEDS["la-metro-rail"]
    assert feed.zip_path == home / "data" / "feeds" / "la-metro-rail.zip"
    assert feed.normalized_zip_path.parent == home / "data" / "feeds"


def test_fetch_downloads_under_the_home_and_nowhere_else(home):
    """The acceptance case: with the variable set, a download lands only there."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("agency.txt", "agency_id,agency_name\nx,X\n")
    resp = mock.Mock(content=buf.getvalue())
    resp.raise_for_status = mock.Mock()
    with mock.patch.object(feeds.requests, "get", return_value=resp) as get:
        path = feeds.fetch("la-metro-rail")
    get.assert_called_once()
    assert path == home / "data" / "feeds" / "la-metro-rail.zip"
    assert path.is_file()
    assert sorted(p.relative_to(home).as_posix() for p in home.rglob("*")) == [
        "data", "data/feeds", "data/feeds/la-metro-rail.zip"]


def test_stage_cache_follows_the_home(home):
    d = pipeline.graph_dir("la-metro-rail")
    assert d == home / "data" / "graphs" / "la-metro-rail"
    assert d.is_dir()


def test_code_paths_stay_in_the_repository(home):
    """The recorder and the site are code; the variable must not move them."""
    assert export.RECORDER == config.REPO_ROOT / "bin" / "_record.js"
    assert export.MAPS_DIR == config.REPO_ROOT / "site" / "src" / "maps"
    assert site.SITE_DIR == config.REPO_ROOT / "site"
    assert config.REPO_ROOT not in home.parents


def test_home_is_read_at_call_time(monkeypatch, tmp_path):
    """A host that sets the variable after import still gets its own home."""
    monkeypatch.delenv(config.ENV, raising=False)
    before = config.out_dir()
    monkeypatch.setenv(config.ENV, str(tmp_path))
    assert config.out_dir() == tmp_path / "out"
    assert before == config.REPO_ROOT / "out"
