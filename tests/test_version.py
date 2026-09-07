"""One version, stated twice: the package and its metadata must agree."""

import re
import subprocess
import sys

from schematic import __version__, config


def test_pyproject_and_package_agree():
    text = (config.REPO_ROOT / "pyproject.toml").read_text()
    assert re.search(r'^version = "(.+)"$', text, re.M).group(1) == __version__


def test_changelog_has_an_entry_for_this_version():
    text = (config.REPO_ROOT / "CHANGELOG.md").read_text()
    assert f"## [{__version__}]" in text


def test_module_prints_the_version():
    out = subprocess.run([sys.executable, "-m", "schematic", "--version"],
                         capture_output=True, text=True, check=True).stdout
    assert out.strip() == f"schematic {__version__}"
