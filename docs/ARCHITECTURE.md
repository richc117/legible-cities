# Architecture

How the engine is put together, for someone about to change it. The README
says what the engine makes and how to use it; this says where each part lives,
what each stage takes and makes, the seams other programs drive, and the traps
that have each cost a debugging round. Paths are relative to the repository
root, and code is under `src/schematic/` unless a path says otherwise.

## What it is

`schematic` is a Python package (distribution name `openschematicmaps`, Python
3.12 or later). It takes a city's GTFS feed, has the University of Freiburg's
[LOOM](https://github.com/ad-freiburg/loom) tools turn its network into an
octilinear one, and from that draws a schematic map as SVG and writes a
self-contained HTML page that runs the day's trains over it.

It is reached three ways, and all three run the same code:

- **as a library**: `pipeline.run`, `pipeline.lay_out`, `feeds.add`,
  `export.plan` and the rest;
- **from `bin/`**: `bin/run-all`, `bin/export`, `bin/build-site` and the
  smaller scripts the README lists;
- **as a JSON-RPC 2.0 server** on stdin and stdout, `python -m
  schematic.serve`, which is how the desktop app drives it.

There is one renderer. The animation page is the viewer everywhere: the site's
atlas links it, the essay embeds it in an iframe, the exporter captures it, and
the desktop app embeds it. Each of them drives it through one object,
`window.__present`, and nothing else draws a map.

## The pipeline

`pipeline.run` reports eight steps in order: `gtfs2graph`, `topo`, `loom`,
`octi`, `schedule`, `render`, `animate`, `write`. The first four are LOOM's and
make the *layout*; the last four are this package's and make a *map* from a
stored layout.

| Step | Takes | Makes | Code |
|---|---|---|---|
| fetch | a registry entry's address, or a zip a person added | `data/feeds/<key>.zip`, downloaded whole or not at all | `feeds.fetch` |
| normalise | the feed zip | `<key>.normalized.zip`: only GTFS tables, `route_short_name` filled in, the agency filter applied | `feeds.normalize` |
| `gtfs2graph` | the normalised feed, and the mode (`-m`) | a GeoJSON line graph of the network as it runs on the ground | LOOM, through `loom.gtfs2graph` |
| `topo` | a line graph | the same, with overlapping track merged into shared edges | LOOM |
| `loom` | a line graph | the same, with the left-to-right order of lines on every edge solved | LOOM |
| `octi` | a line graph | the network snapped to a grid, octilinear by default | LOOM |
| store | the four stage files | `data/graphs/<key>/<id>/` with a `.meta.json`, moved into place whole | `pipeline.lay_out` |
| `schedule` | the `octi` graph, the feed's tables and a service day | trips that run that day, each a list of timed calls on map nodes | `schedule.py` |
| `render` | the `octi` graph, reprojected to Web Mercator | the SVG: one `<path>` per (line, edge) with a stable id, offset parallel tracks, station markers, placed labels | `render.py`, `offsets.py`, `labels.py` |
| `animate` | the drawn tracks, the trips, and the `loom` stage | deduplicated train paths, keyframes, the linear view's columns, the geographic twin, the page's routing rules | `animate.py`, `linear.py` |
| `write` | all of that | `<key>.svg`, `<key>.positions.json`, `<key>.html` (and, from `map.build`, two thumbnails), moved into place with the page last | `pipeline._publish` |

Every LOOM stage reads and writes the same GeoJSON line-graph format on stdin
and stdout (`linegraph.py` is its typed model), so any stage can be read or
drawn on its own. After `loom`, the order of an edge's `lines` array is the
solved order of parallel tracks across the edge, which is what `offsets.py`
draws from.

The four LOOM stages run at LOOM's own defaults. `pipeline.STAGES` holds their
arguments, and the tuples are empty on purpose: a layout's id is computed from
them. A client may tune a layout (`graph.build`'s `tuning`: topo's merge
distance, octi's grid, grid size and penalties), and `pipeline.stages_for`
appends those flags to a copy. A value equal to LOOM's default writes no flag,
so an untuned layout and one tuned only to defaults have the same id.

Exports are a separate step on a built page: `export.plan` describes a capture,
a recorder captures frames (`bin/_record.js`, or the desktop app's own capture),
and `export.encode` turns frames or a still into the deliverable with ffmpeg and
writes a `.json` sidecar beside it.

### LOOM's two backends

`loom.py` runs the tools behind one of two backends, picked on every call by
`SCHEMATIC_LOOM_BIN`:

- **Docker** (unset): each tool in a fresh container from the image
  `docker/Dockerfile` builds, tagged `openschematicmaps/loom`. The Dockerfile
  builds LOOM natively for the host with the open solvers and pins the commit
  as `LOOM_REF`.
- **Native** (set): the four binaries in the directory it names. A build
  without libzip cannot read a zip, so this backend hands `gtfs2graph` the feed
  unpacked into a folder beside it (`loom.unpacked`).

The native binaries carry no version of their own, so the host says which LOOM
commit they are in `SCHEMATIC_LOOM_COMMIT`. `loom.commit()` reads that variable
under both backends, `engine.info` reports it, and it is part of every layout's
id. With Docker and the variable unset, the id carries `null`: the image's
`LOOM_REF` is never read, so rebuilding the image at another ref keeps every
id. A layout stored with the variable set is found by `pipeline.stored` only
while it is set to the same value.

Either way every tool has a timeout (`loom.TIMEOUT`, thirty minutes), its
stderr streams to the request's log, a cancel ends it, and a tool with no
payload gets no stdin at all.

## The home and the cache

`config.py` decides where the engine writes. `SCHEMATIC_HOME` names a folder;
unset, it is the repository root. These are functions, not constants, so a host
that sets the variable after import is seen.

```
<home>/
  data/feeds/
    <key>.zip                        the feed as fetched, or as a person added it
    <key>.zip.part                   a download by feeds.fetch in progress
    .adding-<pid>-<tid>.zip          a feed being added by feeds.add, before it is checked
    <key>.normalized.zip             the copy LOOM and the schedule read
    <key>.<token>.normalized.zip     a copy normalised with overrides (another agency, mode or labels)
    <key>.normalized.tmp             a normalised copy being written
    <key>.normalized/                the copy unpacked, for the native backend
    <key>.<token>.normalized/        a variant's copy unpacked
    <key>.normalized.unpacking/      an unpack in progress
    user-feeds.json                  the feeds a person added
    user-feeds.json.<pid>-<tid>.part the registry being rewritten
  data/graphs/<key>/
    <id>/                            one stored layout:
      00_gtfs2graph.json  01_topo.json  02_loom.json  03_octi.json  .meta.json
    <id>.building-<pid>-<suffix>/    a build in flight
    <id>.old-<pid>/                  a stored set moved aside by a forced re-layout
  out/
    <key>.svg  <key>.html  <key>.positions.json
    <key>-thumb-dark.svg  <key>-thumb-light.svg     (written by map.build)
    <out>/...                        the same, when map.build names a folder
```

What is code stays on `config.REPO_ROOT` whatever the home is: the recorder
(`export.RECORDER`) and the site's folders. `bin/export` writes its files to a
`legible-cities` folder on the Desktop and never into the repository.

Writes are whole or not at all. A download by `feeds.fetch` (a preset's, or
again for a feed a person added by address whose zip has gone) goes to a
`.part` beside the zip, and a feed a person adds (`feeds.add`) to an `.adding-`
staging file, and each is moved into place. The registry is written to a
`.part` of the writer's own and moved over `user-feeds.json`. An unpack for the
native backend goes into an `.unpacking` folder first. A normalised copy is
written to a `.tmp` beside it. A layout is written into its `.building-`
scratch and moved into place. A map's files are written into a
`.<key>.writing-<pid>-` scratch inside the output folder and moved in one by
one, the page last.

What a killed process leaves behind is cleared in three ways:

- **Swept.** Only the layout store's and the maps' scratches are. A build
  sweeps a feed's `.building-` scratches and its `.old-` sets whose place holds
  a whole set again (`pipeline._sweep`, with `_leftover` reading each name
  whole, so a name that only looks like one is never touched). A map's write
  sweeps the `.writing-` scratches in its folder (`pipeline._sweep_writes`).
  Each is swept only when the process named in it has gone; on Windows, where
  another process cannot be asked about, it is kept.
- **Overwritten next time.** A `.part` download, a `.tmp` normalised copy and
  an `.unpacking` folder have fixed names, and the next attempt replaces them.
- **Never removed.** An `.adding-<pid>-<tid>.zip` and a
  `user-feeds.json.<pid>-<tid>.part` are named for their writer, so nothing
  reuses or removes them.

## Layouts, and the determinism rule

**A layout is computed once and stored. Nothing re-runs it implicitly.** Its id
is the sha256 of exactly what went into it (`pipeline.layout_inputs`): the
feed's key and its bytes, the mode, the agency, the two label options, the LOOM
commit (`SCHEMATIC_LOOM_COMMIT`, or null) and every stage's arguments. The
bytes are the zip's as fetched, not the normalised copy's. No version and no date. The same inputs name the same
id before anything runs; a different input names a new layout beside the old
one.

- `map.build` takes a layout id and never lays a feed out. A layout that is not
  stored is refused with the kind `layout`.
- `graph.build` answers from the store when the layout is there, without
  running a tool.
- `force` lays the same inputs out again under the same id. The stored set
  stays readable until the new one is whole; then the old set is moved aside,
  the new one moved in, and the old one removed. If the second move is refused,
  the old set is put back.
- A set left aside by a process that died between those moves is put back the
  next time anything asks for that layout (`pipeline._put_back`).
- One lock (`pipeline._lock`) covers every move of a set and every read that
  must not overlap one. Readers take the bytes they need under it and parse
  outside it, so a map is drawn from one whole set. Windows will not rename a
  folder while a file in it is open, which is why this matters.

The stored layout is kept because it cannot be made again. LOOM's `topo` is not
deterministic: the same binary on the same input can give a different graph,
and upstream confirmed that as expected behaviour
([ad-freiburg/loom#44](https://github.com/ad-freiburg/loom/issues/44)). Two
builds of LOOM can disagree too (`tests/test_loom.py` prints the native and
Docker graphs side by side rather than asserting they match after `topo`). So
the layout a map was drawn from is the only thing that reproduces it.

What is byte-stable and what is not:

- **Not byte-stable: LOOM's JSON.** Node ids are pointer addresses, different
  on every run and between stages. Compare two graphs by their stations'
  coordinates, their edges' endpoints and their ordered line labels, never with
  `cmp` (`tests/test_loom.py`, `_as_graph`). Only `station_id` and
  `station_label` survive from stage to stage as identity.
- **Byte-stable: everything the engine draws from a stored layout.** The same
  layout and the same options give the same SVG, page and positions file; the
  tests pin a Pittsburgh snapshot of each (`tests/fixtures/colors/`,
  `tests/test_colors.py`). Every text file is written UTF-8 with LF line ends
  on every platform. The theme and sample-city thumbnails carry no date, so a
  second run writes the same bytes. `--schema` prints the same bytes on every
  platform.
- **Not byte-stable: rasters.** A capture is reproducible as content, not as
  bytes. The page's clock is stepped by hand, `advance(1/fps)` per frame, after
  `setCapture(true)` has cancelled the page's queued animation frame; without
  that cancel, one real-time frame lands at an unpredictable moment and two runs
  diverge. A still is taken at the export's `at`, else 07:00
  (`export.PAGE_START`), after the clock has been stopped.

### Comparing two renders

Two traps, both of which have given confident wrong answers:

- **Compare in RGB, never RGBA.** `ImageChops.difference(a, b).getbbox()` on
  RGBA images reports the box of non-zero *alpha*. Two opaque frames always
  have the same alpha, so it reports no difference whatever the colours are.
- **Compare against a threshold, never for equality.** Skia does not rasterise
  identically every time: a few antialiased pixels land a level apart. Real
  drift, a train one place along or a morph a frame early, moves a channel by
  tens. The suite's tolerance is 8 of 255 per channel (`DRIFT` in
  `tests/test_determinism.py`).

```python
from PIL import Image, ImageChops
d = ImageChops.difference(*(Image.open(p).convert("RGB") for p in (before, after)))
assert max(band[1] for band in d.getextrema()) <= 8
```

Compare pixels, not file hashes: the PNG container is not always the same bytes
for the same picture.

## Module map

In rough dependency order.

| Module | What it does |
|---|---|
| `config.py` | The home: `SCHEMATIC_HOME`, else the repository root; `feeds_dir()`, `graphs_dir()`, `out_dir()`. |
| `feeds.py` | The feed registry in two halves (the presets in `FEEDS`, a person's feeds in `user-feeds.json`), the download cache, the size limits, and the normalisation. |
| `inspection.py` | `feeds.inspect`: what is in a feed as data, from the raw zip and never `stop_times`. |
| `linegraph.py` | The typed model of LOOM's GeoJSON line graph. |
| `crs.py` | WGS84 to Web Mercator and back. |
| `loom.py` | Runs the LOOM tools behind the Docker or native backend; timeouts, stderr to the log, cancellable jobs. |
| `offsets.py` | Parallel-track geometry: mitred offset polylines, slot offsets, lengths along a path. |
| `labels.py` | Station label placement, with oriented-box collision (separating-axis theorem). |
| `names.py` | Stop-name cleaning, shared by the renderer and the stop matcher. |
| `render.py` | A line graph to SVG; `Style` and its presets; line colours and strokes; `render.stage` for one stored stage; `octilinearity`. |
| `schedule.py` | GTFS to timed trips on map nodes: the service day, active services, headway expansion, stop matching. |
| `linear.py` | The linear view's columns: each line's spine and its branches. Rows are placed in the browser. |
| `animate.py` | Trips to routed, deduplicated paths and keyframes; the geographic twin; fills the page template. |
| `describe.py` | A stage graph as fields a sentence can be written from: each line's ends, stations, branches and where it meets others. |
| `diagnostics.py` | A build's numbers as data, once: the protocol's block, the command line's summary, the caveats and the issue score. |
| `thumbnail.py` | A map's small dark and light pictures, written beside the page by `map.build`. |
| `sample_thumbnails.py` | The same picture of every sample city with a stored layout (`bin/thumbnails`). |
| `theme_thumbnails.py` | Two pictures of the map's themes, drawn from an invented network with no LOOM (`bin/theme-thumbnails`). |
| `pipeline.py` | Ties it together: the layout store, `lay_out`, `stored`, `run`, a layout's service day and its cached minutes. |
| `export.py` | Presets, storyboards, safe zones and palettes; `plan`, `capture`, `encode` and `run`. |
| `site.py` | Builds the maps into the Eleventy site under `site/`, with their provenance. |
| `serve.py` | The JSON-RPC server. |
| `protocol/v1.json` | The protocol's JSON Schema, hand-written; the contract. |
| `page/page.html`, `page/present.js` | The animation page and its presentation mode. |
| `page/icons/` | The four view icons (Phosphor, MIT), copied unmodified. |
| `fonts/` | The two optional label faces (OFL), made by `bin/build-fonts`. |
| `__main__.py` | `python -m schematic --version`. |

## The page and its seam

`page/page.html` is a template, and `animate.texts` fills its placeholders
(`__SVG__`, `__DATA__`, `__ROUTING__`, `__PRESENT__` and the rest) in one pass
that never reads what it has written. `present.js` is inlined at `__PRESENT__`.
The result is one file: its scripts and styles are inline, its images and fonts
are `data:` URIs, and its script fetches nothing, because an export opens it
over `file://` and the site serves it from a subpath. `tests/test_colors.py`
holds the page to that structure.

One exception, which draws nothing: a page built with `icons=` (`pipeline.run`;
the site passes its favicon folder) carries three icon links there (two
`rel="icon"` and one `rel="apple-touch-icon"`), `__ICONS__` in the template, and a browser requests them. A page from
`map.build` is built without, and `__ICONS__` is empty. The full Chromium
requests a site page's icons even when capturing, and under `file://` they are
not there, so `bin/_record.js` ignores that one failure.

The data is JSON inside a `<script>` element. A browser ends that element at
the first `</script` whatever the JSON means, so the characters that could end
it are escaped (`animate._json_for_script`).

### `window.__present`

Everything else in the page is closed over inside its script. This object is
the only way in, and `tests/test_colors.py` checks it offers every method the
protocol and `bin/_record.js` name.

| Method | What it does |
|---|---|
| `advance(dt)` | Steps playback by `dt` seconds and draws: the clock by `dt × speed` while playing and not drawing in, and every tween by `dt`. |
| `seek(sec)` | Sets the clock, held to the day's bounds, and draws. |
| `bounds()` | `{t0, t1}`: when the day's first trip starts and its last ends, in seconds. |
| `setPlaying(on)`, `setSpeed(x)` | Playback, as the header's controls set it. |
| `setCapture(on)` | On: cancels the queued animation frame, so only `advance` moves the clock. Off: real time again. |
| `settle()` | Snaps every tween to its target and draws; returns a promise that resolves once the page's fonts have loaded, refitting the box if a face arrived late. |
| `onDraw(fn)` | One callback at the end of every draw. |
| `setView(v, dur)` | The layout axis: `map`, `linear` or `string` (the Time view's internal name). |
| `setGeo(on, dur)` | The geographic axis. Returns false on a page without the geometry. |
| `showView(name, dur)` | Both axes by the public name: `geographic`, `map` (or `schematic`), `linear`, `time`. |
| `hasGeo()` | Whether the page carries the geographic twin. |
| `setLabels(on)` | Station names on or off. |
| `setRoutes(keep)` | Shows only the lines named, and narrows the box to them. |
| `setFrame(aspect, top, margin)` | Pads the box to an aspect ratio, inside the SVG. |
| `setTheme(name)` | `warm-dark` or `sepia`; false and no change for any other name. Never writes storage. |
| `setTrip(from, to)` | Finds a trip between two stations by the ids `map.build`'s `stations` lists, fades everything off it, and returns what `state().trip` says. `setTrip(null)` clears it. |
| `setCard(on)` | The title card over the frame, cut in or out; draws. |
| `setDrawn(fraction)` | Draws the network in, from 0 (bare ground) to 1 (whole). |
| `state()` | `now`, `clock`, `shown`, `mode`, `view`, `str`, `geo`, `box`, `viewName`, `labels`, `theme`, `trip`, `changePenalty`, `card`, `drawn` and `draws`. |

Two rules hold the seam together:

- **`setView` and `setGeo` are independent axes.** `bin/_record.js` raises the
  geographic axis and then names a view, so a `setView` that lowered it would
  break every geographic export. `showView` is the call that sets both.
- **Durations mean different things on the two axes.** On both, a positive
  duration runs a timed tween. `setGeo` snaps on `0` and eases at the page's
  own rate when the duration is left out; a transition that has gone instant
  is the first place to look. `setView`, and the layout axis of `showView`,
  only set a target: `0` and no duration both ease. What lands a state at once
  is `settle()`, which snaps every tween to its target; `present.js` sets the
  opening view and then calls it.

`draws` counts the page's draws since it loaded. A call can draw more than once,
and some calls (`setTheme`, `setLabels`, `setView`) change the picture without
drawing, so a capture makes every change before its step and reads `draws`
after it.

### Presentation mode

With `?present=1` the page hides its own interface and reads the rest of its
address (`present.js`; `theme` and `strip` are read by the boot script at the
top of `page.html`, before paint).

| Parameter | Meaning |
|---|---|
| `present=1` | Presentation mode. Nothing below but `theme` applies without it. |
| `view` | `geographic`, `map` (alias `schematic`), `linear` or `time`. |
| `labels`, `title`, `clock` | `1` or `0`. Labels default on; title and clock off. |
| `controls=1` | Shows the page's own view switcher. Off by default. |
| `frame=W:H` | Pads the box to that aspect. Without it the frame fills the window and refits on resize. |
| `frametop`, `margin` | Where the added height goes, and the margin, as fractions. |
| `speed`, `at`, `play` | Simulated seconds per second (60 by default), the clock to open at (`HH:MM`), and `play=0` to open paused. |
| `theme` | `sepia`, or the default warm dark. Read with or without `present`, and wins over the theme the site stored. |
| `lines` | Comma-separated line labels to show. |
| `sequence=transform` | Loops geographic, schematic, linear and back, holding 2.6 s and morphing over 1.8 s. Holds the schematic map under reduced motion. |
| `city`, `network`, `date`, `caption` | The words of the title block and the title card. |
| `corner` | The clock's corner: `top-left`, `top-right` or `bottom-left`; bottom right without it. |
| `ztop`, `zbottom`, `zside`, `zrail`, `zrailtop` | A platform's safe zones, as fractions of the frame, written by `export.plan` from `export.SAFE_ZONES`. |
| `safe=1` | Draws those zones as boxes, for a preview. Never in a deliverable. |
| `strip=<px>` | A whole number from 1 to 256: a band that tall below the map, where the page draws `draws` modulo 65536 as 32 black and white blocks, two a bit. The frame lays out in the window less the band. |

**`controls` must stay off for an export.** A capture takes everything painted
over the stage, so a visible control would be baked into every frame, and it
would flicker through its pressed states as each beat changed the view.
`export.url_for` never sets it, and a test holds it to that.

**The header is hidden, never removed.** Every frame writes the clock, the
count and the scrubber; remove those elements and every frame throws.

## The JSON-RPC server

`serve.py`, run as `python -m schematic.serve`.

**Transport.** JSON-RPC 2.0 with the Language Server Protocol's framing
(`Content-Length` headers) on stdin and stdout, through `python-lsp-jsonrpc`.
Logs go to stderr, at the level `SCHEMATIC_LOG` names (`info` by default). The
server runs until stdin closes or `engine.shutdown` is called. One reader thread
reads every message, so `feeds.list` or a cancel is answered while work runs;
long requests run on a pool of four workers.

**Stdout is the protocol.** Once the server starts, `sys.stdout` points at
stderr, so a stray `print` anywhere in the engine cannot land inside a frame.

**Version and fingerprint.** `serve.PROTOCOL` is 1, and `protocol/v1.json`
says `"protocol": 1`. `engine.info` answers the engine's version, the protocol,
Python's version, the LOOM commit and backend, the ffmpeg it found and the home.
`python -m schematic.serve --schema` prints the schema. It writes bytes, not
text, because a text-mode stdout on Windows would turn each newline into CRLF.
The desktop app pins the sha256 of exactly those bytes as the protocol's
fingerprint, generates its types from them, and refuses an engine whose version
or protocol is not the one it pins. So any change to `v1.json`, a description
included, moves the fingerprint. While the protocol is at version 1 every change
is an addition, and one a client would notice is a minor version; a change to
`protocol` itself would be a major one.

`v1.json` is formatted by hand, one line per method. Add to it by hand rather
than by writing out a parsed copy, or the whole file's layout changes.

**Methods**, by group:

| Group | Methods |
|---|---|
| Engine | `engine.info`, `engine.shutdown` |
| Feeds | `feeds.list`, `feeds.add`, `feeds.remove`, `feeds.inspect`, `feeds.service` |
| Layout | `graph.build`, `render.stage` |
| Map | `map.build`, `style.presets` |
| Export | `export.presets`, `export.storyboards`, `export.plan`, `export.encode` |

`graph.build`, `map.build`, `export.encode`, `feeds.service`, `feeds.inspect`,
`feeds.add`, `feeds.remove` and `render.stage` are long requests. They stay
open until done and meanwhile send notifications carrying the request's id:

- `job/progress` `{id, stage, fraction, message}`, plus `layout` on a report of
  one of a layout's stages, so a client can draw that stage with `render.stage`
  before the run ends;
- `job/log` `{id, level, line}`: each stage's own line, and a LOOM tool's or
  ffmpeg's stderr as it is written.

**Cancellation.** A client sends `$/cancelRequest` `{id}`. Depending on what the
request is doing, the cancel ends the process it waits on (a LOOM tool, or
ffmpeg); stops a download between chunks and caches nothing of it; or stops
work that starts no process at the boundary between two steps (`graph.build`
between its tools, `map.build` after each of schedule, render and animate). Work that waits on none of these
runs to its end. A cancelled request is answered with the cancelled error,
-32800.

**The point of no return.** A method that writes what a client sees has one,
immediately before its first such write: `graph.build` moving a layout into the
store, `map.build` moving its files into place, `export.encode` moving the file
and its sidecar into place, `feeds.add` moving its zip into the cache and
writing the registry, `feeds.remove` writing the registry. The check for a
cancel and the commit are one step under one lock (`serve._Job`). A cancel
before it is honoured, and nothing a client sees has been written (a feed
fetched whole on the way stays cached). A cancel after it is too late: the
request runs to its end and its result carries `cancel_too_late: true`. A client
that cancels one of these must accept a result.

**Errors.** An error a person can act on is code -32000 with `data` `{kind,
detail, hint}`. `hint` is the sentence the engine raised, for a person to read;
`detail` says where, for a log. `kind` is one of `params`, `feed`, `loom`,
`schedule`, `export`, `io`, `engine` or `layout`, and `serve.classify` reads it
from the exception and the module that raised it. Bad parameters are -32602 with the same shape and
kind `params`. The server judges parameters as the schema does, and
`tests/test_params_parity.py` drives generated edge cases through both to hold
them together: every pattern is matched whole, a null is not a missing field,
and a number no double holds is refused.

`tests/test_serve.py` checks that what the server sends validates against the
schema. A change to the protocol is a change to `v1.json` first.

## Traps that are about code

**LOOM writes lon/lat but computes in Web Mercator metres.** A 45-degree
schematic edge reads as about 39 degrees at Los Angeles's latitude, and a
correct map measures 37% octilinear instead of 99.9%. Reproject first
(`graph.reproject(crs.to_mercator)`) before measuring an angle or drawing
anything; `pipeline.run` does before it draws.

**Measure octilinearity by length, not by segment count.** LOOM writes six
decimal places, which leaves metre-scale stubs at station nodes whose angles are
rounding noise; over half the segments are under 30 m. `render.octilinearity`
weights each segment by its length.

**Labels need oriented boxes.** Two 45-degree labels are thin strips that slide
past each other while their axis-aligned boxes overlap heavily. Testing
axis-aligned boxes drops about 70% of labels, on exactly the horizontal runs
that most need rotating. `labels.collide` tests the true rotated quads with the
separating-axis theorem, after a cheap axis-aligned pre-filter.

**Never theme a line colour.** With `Style(themed=True)`, the default for a
map, the renderer writes the furniture (ground, station markers, labels) as
`var(--map-*, literal)` and every line stroke as a literal hex colour. The
obvious way to put a map on a dark page, `filter: invert()`, also inverts the
agencies' colours: LA's A Line turns from `#0072bc` to orange. The literal
fallbacks keep a standalone SVG the same bytes either way.
`tests/test_theming.py` guards this.

**In the server, stdout is the protocol.** `serve.main` points `sys.stdout` at
stderr. `loom.execute` gives a tool with no payload `stdin=DEVNULL`, never the
parent's stdin, which `docker run -i` would otherwise read: the client's own
requests. The Docker backend runs each container with `--init`, so a cancel's
SIGTERM reaches the tool instead of a PID 1 that ignores it.

**`gtfs2graph` labels lines with `route_short_name`.** A feed that leaves it
blank (LA names its routes "Metro A Line") gives a graph whose every line is
labelled `""`. `feeds.normalize` fills it in from `route_long_name` through the
registry's `label_pattern`, and strips `label_strip` (BART publishes one route
per direction). The schedule reads the same normalised copy through
`feeds.tables`, so the graph and the timetable cannot disagree about a label.
Never read the raw zip from `feeds.fetch` for anything that must match the map.

**Filter the schedule by the graph's lines.** The graph is built for one mode,
so a feed that carries buses and trains must have its trips restricted to the
map's lines too, or tens of thousands of bus trips will not route.
`schedule.trips_on` takes the lines.

**`topo` merges interchange platforms into one node** and keeps one of their
stop ids. `schedule.match_stops` therefore matches a stop by its `station_id`,
then its parent station, then its normalised name, and breaks a tie by which
lines each candidate carries: BART has two stations called Coliseum, and
sending mainline trains to the airport connector's platform strands them.

**Times past midnight stay past midnight.** `25:44:00` is 1:44 the next
morning, stored as seconds after the service day's midnight. Wrapping it sends
the night's last trains back to dawn.

**The keyframe is a stop index, not a distance.** A trip's keyframes are
`[time, stop index]` pairs, fractional between stops. Each view turns that one
number into geometry: arc length along the drawn path for the map, a position on
a row for the linear view, a point on a diagonal in the time chart. So the views
cannot drift apart, and a morph between two is exact at both ends.

**Pair the geographic twin against `02_loom.json`, never `00_gtfs2graph.json`.**
`octi` only moves vertices, so the `loom` stage has the same stations and the
same solved line order, and `animate.geographic_tracks` can pair each drawn
track with its twin by `station_id`. `gtfs2graph`'s graph has neither.

**The page's icons are copied, not redrawn.** The four view icons are
Phosphor's, under MIT, copied unmodified into `page/icons/` and inlined as
`data:` URIs; their `LICENSE` sits beside them.

## Real feeds

What the GTFS specification describes and what agencies publish differ. Each of
these has a step in the code, and most have a test.

| What feeds do | What handles it |
|---|---|
| Ship prose beside the tables (SFMTA's licence agreement is a `.txt`) | Only names in `feeds.GTFS_TABLES` are read. |
| Run on headways, not a timetable (Mexico City's template trips all start at `00:00:00`) | `schedule.expand_trip` turns each `frequencies.txt` window into runs; a trip with no window keeps its own times. `tests/test_schedule.py`, `tests/test_cdmx.py`. |
| Carry several operators that `route_type` cannot separate | `Feed.agency`; the filter cascades through trips, stop times and frequencies, since dropping routes alone leaves orphans LOOM rejects. `tests/test_feeds.py`. |
| Expire (Mexico City's calendar ended in December 2025) | `schedule.busiest_weekday` scans from the window's middle when the anchor is outside it, rather than clamping onto an edge. `tests/test_schedule.py`. |
| Span years with nothing running at the start (Miami) | `busiest_weekday` scans from the anchor, then the window's start, then samples the whole window. |
| Have no `calendar.txt` (Phoenix, NJ Transit, LIRR) | `schedule.service_window` and `active_services` read whichever table exists. `tests/test_schedule.py`. |
| Pad their headers (Metra's `" trip_id"`), or start with a byte-order mark | Every read uses `skipinitialspace` and strips each column name. |
| Hold a table LOOM's parser rejects (MBTA's `pathways.txt`, a negative `stair_count`) | `feeds.LOOM_SKIP` drops `pathways`, `translations` and `attributions` from the normalised copy. It keeps `levels`, which `stops.txt` references. |
| Refuse a default user agent (MARTA answers 403) | `feeds._download` sends its own. |
| Disagree about `route_type` for the same thing | Nothing can; check `routes.txt` before choosing a `mode`. A mode that matches nothing gives an empty graph, and `pipeline.require_edges` says so by name. |
| Publish colours that are not colours | A feed's `route_color` is held to six hex digits, else the default colour. `tests/test_render_escaping.py`. |
| Name a line or station with markup | Names reach the SVG escaped, the page's data escaped for a `<script>` element, and the page's chips as DOM text. `tests/test_page_escaping.py`, `tests/test_page_hostile_labels.py`. |

## Limits

**Feed sizes** (`feeds.py`, the `MAX_` constants). A feed may be at most 500 MB
to download, its tables may unpack to at most 1 GB each and 2 GB together, and
its zip may hold at most 10,000 files in a central directory of at most about
10.5 MB. A download is counted as it streams and stopped at the limit. The sizes
are read from the zip's headers before anything is opened, then counted again as
each file is read 64 kB at a time, because a header can lie. A table compressed
with bzip2 or LZMA is refused when the zip is opened (`feeds._check_sizes`); a
table in `LOOM_SKIP`, which the engine never reads, is judged only if
something reads it. These bound the bytes read
out of a zip, not the memory pandas takes once a table is parsed, which is
several times the table's size.

**Timeouts.** Each LOOM tool has thirty minutes (`loom.TIMEOUT`). A download's
connection and each read wait up to 180 seconds. ffmpeg, ffprobe (`export._duration`),
the recorder and `rsvg-convert` have no timeout of their own; a cancel ends
ffmpeg.

**A GIF's plan** reads the positions file beside its page for the line colours,
at most 32 MB.

**What the engine does not restrict.** `feeds.add` fetches any `http://` or
`https://` address it is given, on any host. Refusing loopback, private and
link-local addresses is the caller's job; the desktop app does it before it asks
the engine. A person's feed address is redacted in error text and in an export's
sidecar (`feeds.shown`): the user information, every query value and the
fragment become `<redacted>`. A token carried as a path segment is not redacted,
and neither is the protocol library's own output at `SCHEMATIC_LOG=debug`.

**What the engine does not read.** OpenStreetMap: the registered feeds all ship
`shapes.txt`. A feed with poor or missing shapes can be run through
[pfaedle](https://github.com/ad-freiburg/pfaedle) first, which makes the result
ODbL.
