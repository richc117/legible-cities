# Changelog

What changed in the engine, for people. The format is [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/); the versions are
[semantic](https://semver.org/spec/v2.0.0.html) and each is a git tag
(`v0.2.0`), which is how the desktop app pins the engine it runs.

## Unreleased

### Fixed

- **A feed's text can no longer end the page's data element.** The animation
  page carries its data inside a `<script>` element, and a browser ends that
  element at the first `</script` it sees, whatever the surrounding text
  means. Station and line names come from an agency's GTFS feed, so a name
  containing one would have ended the data early and left the rest of it as
  live markup, in the desktop app and on every published page. The characters
  that can end an element are now escaped; they are valid inside a JSON
  string and parse back unchanged, so no name is altered and nothing
  downstream sees a difference. Published pages should be regenerated.

## [0.2.0] - 2026-09-07

The engine learns to be run by something other than a person at a prompt.

### Added

- `SCHEMATIC_HOME`: where feeds, cached graphs and output live. Unset, the
  repository as before; set, that folder and nothing else. `schematic.config`
  holds the paths, as functions.
- `python -m schematic.serve`, a JSON-RPC 2.0 server on stdin and stdout with
  `Content-Length` framing: `engine.info`, `graph.build`, `map.build`,
  `engine.shutdown`; `job/progress` and `job/log` notifications while a
  request runs; `$/cancelRequest` ends the LOOM process it is waiting on.
  `map.build` requires the service day; the engine never picks one.
  `--schema` prints the protocol's JSON Schema (`schematic/protocol/v1.json`),
  the contract the app's types are generated from.
- `pipeline.schematize()` and `pipeline.run()` take a `progress` callback,
  called once per stage.
- LOOM runs are cancellable (`loom.Job`, `loom.cancellable()`), and a tool's
  stderr streams to the job as it is written instead of arriving at the end.
- `python -m schematic --version`, and this changelog.

### Changed

- Runtime dependencies are what the package imports: pandas, requests and
  python-lsp-jsonrpc. JupyterLab and matplotlib moved to the `notebooks`
  extra; pytest, Pillow and jsonschema to `dev`. `uv.lock` pins the set.
- `pipeline.GRAPH_DIR`, `pipeline.OUT_DIR`, `feeds.REPO_ROOT`, `feeds.DATA_DIR`
  and `feeds.FEED_DIR` are gone; use `config.graphs_dir()`, `config.out_dir()`,
  `config.REPO_ROOT` and `config.feeds_dir()`.
- A LOOM tool given no payload gets no stdin at all rather than the parent's.

## [0.1.0] - 2026-09-07

The first tagged version: the pipeline as it stood when the site launched on
2026-08-30, plus what landed in the week after, now under a licence.

### Added

- The licence: GPL-3.0-or-later for the code; the essay, the station-icon
  photograph and the Calcite icons keep their own terms (README, Licence).
- The geographic view as a first-class one on every animation page, and a
  storyboard that exports the geographic-to-schematic sequence.
- The service day in an exported title; thumbnails for pasted links; the
  essay's second figure exported by the same workflow as the first.
- Before that, and unversioned: `gtfs2graph → topo → loom → octi` through
  Docker, the renderer, the schedule matcher, the animation page with its
  four views, the export presets and storyboards, the atlas of 22 networks,
  and the site.
