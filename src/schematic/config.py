"""Where the engine reads and writes.

Code lives in the package; data lives under a *home* the engine is told
about. ``SCHEMATIC_HOME`` names it. Unset, the home is the repository root, so
the notebooks, ``bin/`` and the site keep writing where they always have:
``data/feeds``, ``data/graphs`` and ``out/`` beside ``src/``.

An installed application cannot write beside its own code, so it sets the
variable to a folder it owns and the engine never touches anything else. The
paths that *are* code -- the recorder script, the site's map folder -- stay
anchored to the repository whatever the variable says; ``REPO_ROOT`` is for
them.

These are functions rather than constants so a test, or a host that sets the
variable after import, sees the current value.
"""

from __future__ import annotations

import os
from pathlib import Path

ENV = "SCHEMATIC_HOME"

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]


def home() -> Path:
    """``$SCHEMATIC_HOME`` if set and non-empty, else the repository root."""
    value = os.environ.get(ENV, "")
    if not value:
        return REPO_ROOT
    return Path(os.path.expanduser(value)).absolute()


def feeds_dir() -> Path:
    """Downloaded and normalised GTFS zips."""
    return home() / "data" / "feeds"


def graphs_dir() -> Path:
    """LOOM's output, one folder per feed, one file per stage."""
    return home() / "data" / "graphs"


def out_dir() -> Path:
    """Rendered maps and animation pages, unless a caller says otherwise."""
    return home() / "out"
