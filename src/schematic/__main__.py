"""``python -m schematic --version``: the version, for a shell or a build script.

The engine's real entry points are the library (``from schematic import
pipeline``) and the server (``python -m schematic.serve``); this one exists
so the version can be read without importing either.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m schematic",
        description="Schematic transit maps from open data. Run the pipeline from "
                    "Python (see README.md) or serve it with python -m schematic.serve.")
    parser.add_argument("--version", action="version", version=f"schematic {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
