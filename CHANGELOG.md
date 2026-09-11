# Changelog

What changed in the engine, for people. The format is [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/); the versions are
[semantic](https://semver.org/spec/v2.0.0.html) and each is a git tag
(`v0.2.0`), which is how the desktop app pins the engine it runs.

## [Unreleased]

### Added

- **Line colours as inputs** (E06). `pipeline.run(..., colors=, default_color=)`
  overrides a line's colour by label over the feed's `route_color` and sets
  the colour of a line the feed leaves uncoloured, resolved once in
  `render.line_colors` for the map and the page together; `map.build` takes
  both as `colors` and `default_color`, written `#rrggbb` (`HexColor` in the
  schema) and refused otherwise. Every drawn line now has an entry in the
  page's `data.lines`, so an uncoloured line (Pittsburgh's inclines) is the
  same colour on the map, in the chips, under the train dots and in the
  time chart, where each element used to fall back on its own. The SVG is
  unchanged for the same inputs. Additive at protocol 1.
- **Diagnostics as data** (`schematic.diagnostics`, E05). `Result.diagnostics()`
  builds one `Diagnostics` from a build; `to_dict()` is the block `map.build`
  has always sent, unchanged; `summary()` renders the lines the command line
  prints, byte for byte as before; `caveats()` and `issue_score()` are the
  atlas's sentences and its ordering number, moved here from the site, which
  now reads them. `map.build`'s result gains `caveats` and `issues` beside
  `diagnostics`, so the desktop app shows the same words and the same number
  as the site. Additive at protocol 1.

### Fixed

- **Coach is its own type to gtfs2graph.** `-m bus` drops a 200-series
  route and `-m coach` keeps it, checked against the tool; the inspection
  now names the 200 series `coach` with `coach` as its mode, rather than
  folding it onto bus and listing `coach` as a name that keeps buses.

## [0.7.1] - 2026-09-11

### Added

- **`feeds.inspect` names the LOOM mode per route type** (`route_types[].mode`,
  and `modes`, every `-m` name that keeps the type), so a client can show
  which of a feed's types a chosen mode keeps without carrying the table
  itself; null and empty for a type LOOM has no name for. Additive at
  protocol 1.
- **An empty `agency` on `graph.build` means every operator.** A missing
  one means the registry entry's, as before, so a feed whose entry names
  an operator had no way to be drawn whole; `feeds.NO_AGENCY` is the word
  for it in the library.

## [0.7.0] - 2026-09-11

### Added

- **A feed registry in two halves.** The presets stay in `FEEDS`; the feeds a
  person adds are kept in `user-feeds.json` beside the zips under the home,
  so they survive a restart. `feeds.all()` is both, `feeds.get(key)` looks
  in both and raises `FeedError` with a sentence otherwise, and every reader
  of the registry goes through them. `feeds.add(source, key=, name=, mode=,
  agency=)` takes a file or a URL, checks the zip for `stops`, `routes`,
  `trips`, `stop_times` and a calendar in one of its two forms, names the
  feed after its first agency and keys it by a unique slug, and leaves
  nothing behind on refusal; `feeds.remove(key)` forgets a user feed with
  its zips and stored layouts, and refuses a preset. `Feed.to_dict()` and
  `Feed.from_dict()`; `Feed.source` says which half. Library only: the
  protocol grows with E09c.
- **`feeds.inspect(key, anchor=)`**, what is in a feed as data, before
  anything is laid out (`schematic.inspection`): the agencies; every route
  with its names, the label the map would use, its type, colours and trip
  count; a histogram of route types with their GTFS names; stops by
  location type; the tables present; the service window and the engine's
  day from the anchor, as `feeds.service` answers them; a suggested LOOM
  mode from the types; and warnings as sentences, for a headway-based
  timetable, an expired or unstarted calendar, a `calendar_dates.txt`-only
  schedule, routes without short names and a feed several operators share.
  Reads the raw zip, never `stop_times`, so New York inspects in seconds.
- **`render.stage(key, stage, layout=, width=, labels=)`** (E15): one
  stored stage graph, drawn as SVG with its counts, for a geographic view
  beside the schematic map; by layout id or by the registry entry, with a
  hint naming what builds a stage that is not stored. `render.summary()`
  is the counts alone.
- **The registry over the protocol** (E09c): `feeds.list` (every entry,
  preset or user, with `cached`), `feeds.add` (a URL or an absolute path;
  a long request reporting the download's bytes, then the check; a cancel
  between chunks leaves nothing), `feeds.remove` (a preset is refused with
  kind `feed`), `feeds.inspect` (the inspection, from an anchor) and
  `render.stage` (by layout id; a stage not stored is kind `layout`).
  Additive at protocol 1; the errors keep `kind: feed` and the sentences
  `feeds.py` writes.

## [0.6.0] - 2026-09-10

### Added

- **`feeds.service` over the protocol.** `{key, anchor?, lines?}` answers a
  feed's service window and the busiest weekday scanning from the anchor,
  which is the engine's today when omitted and is echoed either way, so a
  client can store the choice and reproduce it. With `lines` the trips are
  counted on the map's lines, as the map build counts them. A long request:
  it downloads the feed when it is not cached, reads the calendar and the
  trips (not `stop_times`), and honours a cancel when the read ends.
  Additive at protocol 1.
- **A feed is downloaded whole or not at all**, to a `.part` beside it and
  moved into place, under one lock per feed: a quit mid-download no longer
  leaves a truncated zip that the next call takes for the feed, and two
  requests for one feed no longer write the same file at once.

### Changed

- **`busiest_weekday` takes its anchor as an argument.** It read the clock;
  now the caller passes the day to scan from, and the same feed and anchor
  give the same day on every machine, on any day it is asked. The mid-window
  fallback for an anchor outside the window stays. `pipeline.run` passes
  `anchor=` through and uses today without one, so `bin/run-all` and the
  site behave as before.

## [0.5.0] - 2026-09-10

### Added

- **Layouts addressed by their inputs.** A layout is stored under the home
  at `data/graphs/<feed>/<id>/` with a `.meta.json` beside its four stage
  files, and `<id>` is the sha256 of everything that went into it: the
  feed's bytes, the mode, the agency, the label options, the LOOM build and
  the stage arguments. The same inputs name the same layout before anything
  runs; a change to any of them names a new one and leaves the old
  untouched. A layout is written whole into a scratch directory and moved
  into place, so a cancel or a failure leaves nothing that looks finished,
  and `force` replaces a stored layout only once the new set is whole. A
  set from before layouts had names is migrated under its id, once, in
  place. This is the determinism mechanism the desktop app's ADR-023 named
  and its ADR-027 had to do without.
- **`graph.build` takes `mode`, `agency`, `label_pattern` and
  `label_strip`**, defaulting to the registry entry, and answers with the
  layout's `layout` id and `meta`.

### Changed

- **`map.build` takes `layout` and never lays a feed out.** The id is
  required, a layout that is not stored is refused with a hint to lay the
  feed out first, `force` is gone, and the result names the layout it drew
  from. A client that called `map.build` without a layout has to lay out
  first now. The protocol number stays at 1, as the convention has it for
  a change a client notices; the tag is the gate, and the desktop app pins
  the next one. `ErrorData.kind` gains `layout`, for a layout named that is
  not stored: nothing ran and nothing failed.
- `pipeline.run` takes `layout=`; `pipeline.schematize` keeps its shape and
  gains the overrides; `pipeline.lay_out` and `pipeline.stored` are the
  layout's own functions; `feeds.normalize` and `feeds.tables` take a `Feed`
  as well as a key, and a feed with overrides gets a normalised copy of its
  own.

## [0.4.0] - 2026-09-10

### Added

- **A native LOOM backend.** `SCHEMATIC_LOOM_BIN` names a directory of the
  four tools as binaries and the engine runs those instead of the Docker
  image, which is how the desktop app runs LOOM on a machine without Docker.
  A native `gtfs2graph` is handed the feed unpacked beside its zip, because
  the app's build has no libzip. `SCHEMATIC_LOOM_COMMIT` tells the engine
  which LOOM commit the binaries came from, since they cannot say, and
  `engine.info` reports it with the backend. The protocol is unchanged:
  `engine.info.loom` already had both fields.
- **Every LOOM tool has a timeout** (thirty minutes unless the call says
  otherwise), which ends the process and raises with the tail of its
  stderr. On Windows the tools run without a console window.

### Changed

- **`docker/Dockerfile` pins `LOOM_REF`** to the commit the desktop app
  builds its binaries from, so the two backends run the same LOOM;
  `--build-arg LOOM_REF=<ref>` overrides it.
- **`loom.execute`** is the one runner both backends share; `run`,
  `pipeline`, `gtfs2graph` and `transitmap` keep their signatures and gain
  a `timeout` keyword.

## [0.3.0] - 2026-09-10

The export module is three halves, so another process can do the middle,
and two of them are on the protocol. The protocol stays at version 1: every
change is an addition, and a client of 0.2.1 sees nothing different.

### Added

- **`export.presets`, `export.storyboards`, `export.plan` and
  `export.encode` over the protocol.** The two tables as data; a plan for a
  preset, pure and instant, taking the page's own address and the service
  day when the caller knows them (the desktop app does) and refusing what
  `bin/export` refuses; and the encode of a directory of frames the client
  captured, or its still, into the deliverable with its sidecar, taking the
  client's provenance for the sidecar. `export.encode` is a long request:
  ffmpeg's own frame count arrives as `job/progress`, and `$/cancelRequest`
  ends ffmpeg and leaves no partial file. There is no `export.capture`; the
  desktop app captures for itself. Additive: the protocol stays at 1, the
  schema grows sixteen definitions, and the server checks a plan handed back
  field by field before it trusts it.

### Changed

- **`export.plan`, `export.capture` and `export.encode`.** A plan describes an
  export and is pure: no file, no browser, no reference to the recorder.
  `capture` runs the recorder; `encode` turns a directory of frames, or a
  still, into the deliverable and writes its sidecar, and never writes into
  the frames it was given. `export.run` composes them; `bin/export` is
  unchanged in what it takes and what it writes. The desktop app asks for a
  plan and an encode over the protocol and captures for itself.
- **One ffmpeg.** `SCHEMATIC_FFMPEG` names the ffmpeg every encode runs, with
  ffprobe beside it; before, `engine.info` reported the variable while the
  encoder ignored it.

### Fixed

- **A still is reproducible.** The recorder now stops the page's clock before
  the settle wait in both modes and, for a still, seeks to the clock the
  export asked for (`--at`, or the page's own 07:00), because the page's loop
  has already run for the few frames between load and the recorder's first
  word. A still exported before this landed wherever wall-time put the
  trains; two exported now are the same picture. Every existing still differs
  from a fresh one for that reason.

## [0.2.1] - 2026-09-08

One fix, and it matters wherever a page is published.

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
