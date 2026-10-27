# Contributing

`docs/ARCHITECTURE.md` is how the engine is put together: the pipeline's
stages, the cache, a map of the modules, the page's seam, the JSON-RPC server,
the determinism rule and the traps. Read it before changing any of those.

## Set up

You need Python 3.12 or later and git. [uv](https://docs.astral.sh/uv/) is the
easy way; plain `pip` works too.

```bash
git clone https://github.com/richc117/legible-cities
cd legible-cities
uv sync --locked --extra dev        # a .venv with the package and the test tools, as uv.lock pins them
```

Without uv:

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"   # the same packages, at the newest versions pyproject.toml allows
```

The `notebooks` extra adds JupyterLab and matplotlib, for `notebooks/`.

That is enough to run the suite. Three more things light up more of it:

- **Docker and the LOOM image**, for anything that lays a feed out:
  `docker build -t openschematicmaps/loom docker/`. It compiles LOOM, which
  takes a few minutes. The native binaries are the other way to run LOOM; the
  README's Setup section says how.
- **Node 22 and the site's tools**, for the tests that drive the page in a
  browser: `cd site && npm ci && npx playwright install chromium` (on Linux,
  `--with-deps` as well, for the browser's system libraries). It is the full
  Chromium on purpose, never the headless shell: their rasterisers differ.
- **ffmpeg and ffprobe on `PATH`**, for the encode tests.

## Test

```bash
uv run pytest -q -rs                # or .venv/bin/python -m pytest -q -rs
```

`-rs` lists every test that skipped and why. A test skips, rather than fails,
when what it needs is not there: Docker and the image, a feed already
downloaded, a layout already stored, a page already built, Playwright, ffmpeg,
or a POSIX system. So a green run on a fresh checkout says nothing about the
skipped half; read the list.

To fill in the data the skipped tests need, run the pipeline once. With Docker
and the image:

```bash
uv run bin/run-all la-metro-rail pittsburgh-t cdmx-metro
```

That downloads the three feeds under `data/feeds/`, stores a layout for each
under `data/graphs/`, and writes their maps to `out/`. Most of the real-data
tests read Los Angeles, the colour snapshot reads Pittsburgh, and
`tests/test_cdmx.py` reads Mexico City. `bin/run-all` with no argument does all
22, and a few tests read New York or Metra when they are there.

The browser tests in `tests/test_determinism.py` also need the LA page built
where the site keeps its maps, which is gitignored:

```bash
uv run python -c "from pathlib import Path; from schematic import pipeline; pipeline.run('la-metro-rail', out_dir=Path('site/src/maps'))"
```

(`bin/build-site` builds it too, with the whole site, and redraws the site's
committed link-preview card on the way.)

The LOOM runner's own tests (`tests/test_loom.py`) drive stand-in tools that
are shebang scripts, so on Windows they skip, and the spawn path there is
exercised only by the desktop app's tests. The one that holds the native binaries against the
Docker image needs `SCHEMATIC_LOOM_BIN` naming them as well, and prints its
comparison under `-s`.

### What CI runs, and what it does not

`.github/workflows/test.yml` runs on every pull request and every push to
`main`, on Ubuntu 24.04 with Python 3.12: `uv sync --locked --extra dev`, the
site's tools and the full Chromium, `pytest -q -rs`, and then
`bin/audit-page` (axe, in both themes) over a page drawn from the tests'
hand-made network.

It does **not** have the LOOM image, any real feed, any stored layout, or
ffmpeg. Everything that needs one of those skips there: every test
that lays out or draws a real network, the LOOM backend tests, the
determinism capture over the LA page, and the encodes. A change to any of
those is checked only by running the suite where they are.

`gitleaks.yml` and `preflight.yml` scan every push and pull request too; see
"Before you push".

### Before a change to the pipeline

A change to the feeds, the LOOM runner, the layout store, the schedule, the
renderer, the animation or the server touches code CI cannot exercise on real
data. Before committing one:

1. Build the LOOM image and store the three layouts above, with
   `SCHEMATIC_HOME` unset: a few tests look for a stored layout under the
   repository's own `data/` whatever the home is.
2. Run the whole suite with `-rs`, and check the skip list holds nothing but
   what your machine genuinely lacks. In `tests/test_pipeline.py`,
   `tests/test_cdmx.py`, `tests/test_theming.py`, `tests/test_geographic.py`,
   `tests/test_colors.py`, `tests/test_loom.py` and `tests/test_serve.py`,
   nothing should skip for want of the image, a feed or a layout any more.
   Some of their tests still skip for reasons of their own: the native
   binaries (`SCHEMATIC_LOOM_BIN`), ffmpeg, a site built with New York, or a
   POSIX system.
3. If the page or the export changed, install the site's tools and ffmpeg and
   build the LA page, so that `tests/test_determinism.py`,
   `tests/test_capture_strip.py`, `tests/test_overlay.py`,
   `tests/test_export.py` and `tests/test_gif_palette.py` run too, and run
   `node bin/audit-page out/la-metro-rail.html`.
4. Run `bin/run-all` over every feed and compare its table with the one before
   your change: stations, lines, trips, unrouted trips, matched stops and
   dropped labels. A count that moved is a regression until you can say why.
   On Los Angeles the unrouted and borrowed-track counts should be zero.

A layout is never re-run implicitly. A change to what its id hashes (the
stage arguments, the mode, the agency, the label options, the LOOM commit)
names new layouts beside the stored ones. A change to anything else on the way
to a layout, the LOOM runner or the normalisation among them, names none: lay
the feeds out again with `pipeline.run(key, force=True)`, or delete
`data/graphs/<key>/`. After a change to the normalisation, also delete
`data/feeds/<key>*.normalized.zip`, because a forced layout reuses a
normalised copy that is newer than its zip. The id carries
`SCHEMATIC_LOOM_COMMIT`'s value, so a layout stored with it set is found only
while it is set to the same value.

## Run

```python
from schematic import pipeline
print(pipeline.run("la-metro-rail").summary())
```

`python -m schematic.serve` runs the JSON-RPC server on stdin and stdout, and
`python -m schematic.serve --schema` prints the protocol. The README's Use and
Making assets sections cover the rest of `bin/`.

## Issues

The work is planned on this repository's issues, one unit of work each,
with a label for the phase, the type, the area and the size, and a
milestone per phase. The `E04a` at the front of a title is a reading aid,
not something a tool checks. Pick one, say you are taking it, and keep
the change to what it asks.

## Changes

A contributor opens a pull request; the maintainer commits to `main`.
Either way the history is the record, so the message says why. A
change to the JSON-RPC protocol is a change to `schematic/protocol/v1.json`
first, and `tests/test_serve.py` holds the server to it. Anything the desktop
app needs from the pipeline is a change here, versioned and tagged, never a
copy over there.

A commit message has a subject in the imperative ("Refuse a feed over the
size limit"), under 72 characters, and a body that says why: what was wrong
or missing, and what is true now. A `Co-Authored-By` trailer is welcome where
someone, or a tool, wrote part of the change. Leave out any trailer or link
that points at a tool's session. A change a person would notice gets a line
in `CHANGELOG.md` under `[Unreleased]`.

Contributions are made under the repository's licence, GPL-3.0-or-later
(`LICENSE`). Anything copied in from elsewhere keeps its own terms and is
listed in the README's Licence section, as the icons and the fonts are.

The desktop app ships this repository's `uv.lock`, so a Python package moves
here first. Dependabot proposes the updates (`.github/dependabot.yml`), and
its pull requests are a signal, not a merge: the maintainer makes the bump on
`main` with `uv lock --upgrade-package <name>`, runs the suite, and pushes
both remotes.
Dependabot closes its pull request once the lock has the version.

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

Releasing is the maintainer's. The desktop app pins the engine by git tag and refuses to run against any
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
