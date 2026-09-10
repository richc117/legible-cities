# Contributing

The README's Setup section gets you a working checkout: `uv venv && uv pip
install -e ".[dev,notebooks]"`, Docker for LOOM, `pytest` for the tests. The
tests that run LOOM need Docker and the image and skip without them.

## Issues

The work is planned on this repository's issues, one unit of work each,
with a label for the phase, the type, the area and the size, and a
milestone per phase. The `E04a` at the front of a title is a reading aid,
not something a tool checks. Pick one, say you are taking it, and keep
the change to what it asks.

## Changes

Commit to `main`; the history is the record, so the message says why. A
change to the JSON-RPC protocol is a change to `schematic/protocol/v1.json`
first, and `tests/test_serve.py` holds the server to it. Anything the desktop
app needs from the pipeline is a change here, versioned and tagged, never a
copy over there.

## Releasing

The desktop app pins the engine by git tag and refuses to run against any
other, so a tag is a promise about what `engine.info` will say.

1. Bump `__version__` in `src/schematic/__init__.py` and `version` in
   `pyproject.toml` (they must agree; `tests/test_version.py` checks), and
   run `uv lock`.
2. Add the entry to `CHANGELOG.md`: what changed, for people, under Added,
   Changed, Removed or Fixed.
3. Commit, then tag: `git tag -a v0.2.0 -m "v0.2.0"`.
4. Push the branch and the tag to both remotes. Check from a fresh
   environment that
   `pip install "openschematicmaps @ git+https://github.com/richc117/legible-cities@v0.2.0"`
   installs and `python -m schematic --version` prints the version.

The version is semantic. A change to the protocol that an existing client
would notice is a minor bump while the protocol is at version 1; a change to
`protocol` itself is a major one.
