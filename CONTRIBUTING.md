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

## Before you push

This is a public repository, so two scanners run on every commit and again
in CI: `gitleaks` for keys and tokens, and `bin/preflight` for what a
secret scanner does not know - machine paths, personal addresses, private
hosts, links to tool sessions, and a keyword in a commit message that would
close an issue (name the issue without the `#`; the number belongs in a
pull request or on the issue). Install the hooks once per checkout:

    pip install pre-commit        # or your package manager's equivalent
    pre-commit install

`pre-commit run --all-files` runs them without committing. The `gitleaks`
hook reads only what is staged; `gitleaks dir .` reads the whole working
tree. `.gitleaks.toml` holds the scanner's false positives, each with a
reason, and `.preflight-allowlist` holds the commits already in the history
that the rule arrived too late for. Nothing new goes on either list without
a reason beside it.

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
