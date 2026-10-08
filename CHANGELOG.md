# Changelog

What changed in the engine, for people. The format is [Keep a
Changelog](https://keepachangelog.com/en/1.1.0/); the versions are
[semantic](https://semver.org/spec/v2.0.0.html) and each is a git tag
(`v0.2.0`), which is how the desktop app pins the engine it runs.

## [0.12.0] - 2026-10-07

### Added

- **`map.build` takes an optional `style` object** (issue 36). Its numeric
  fields are `line_width` (1 to 24), `line_gap` (1 to 3, a multiple of the
  line width), `station_radius` (1 to 20), `interchange_radius` (1 to 30),
  `station_stroke` (0 to 8), `label_size` (6 to 32), `label_offset` (0 to 40)
  and `padding` (0 to 200). All but `line_gap` are in one unit, SVG user units
  at the map's width: the map is fitted to `width`, 1,800 by default, so a line
  width of 7 is seven of 1,800. The four colours `background`, `station_fill`,
  `station_stroke_color` and `label_color`, each written `#rrggbb`, are
  accepted for the command line and the site; the desktop app does not send
  them, because the page's theme owns the furniture. Omitting `style`, or
  sending `{}`, draws exactly what was drawn before. A field outside its range,
  a key that is not on the list and an `interchange_radius` below
  `station_radius` are refused with the `params` kind and a sentence naming the
  field; the radius rule is judged on the values the map would be drawn with,
  so a `station_radius` above 6 sent alone is refused as well. `label_size` and
  `label_offset` re-place the labels and move the `viewBox`, never the stored
  layout. The geographic layer now follows the map's track spacing, where it
  was spaced by the default style whatever the map was drawn with.
  `MapBuildParams` gained `style` (`$defs/MapStyle`).
- **`export.encode` takes an `alt` of a person's own words** (issue 41).
  `Provenance` gained an optional `alt`: a description of the file that the
  sidecar carries trimmed and otherwise verbatim, in place of the sentence the
  engine generates. Omitted, the sidecar is written exactly as before, byte for
  byte. The bound is 1,000 characters, counted as Unicode code points, since
  the platforms' published limits run from 100 to 2,000 and a smaller field
  would cut what a person pastes into it. An empty or blank `alt` is refused
  with a sentence saying to omit it, and one over the bound is refused with the
  count and the 1,000 in the sentence.
- **`export.plan` takes a storyboard written as a list of beats** (issues 39
  and 31). `ExportOptions.storyboard` was one of eight names. It is now a name
  or a list of 1 to 16 beats, written as `export.storyboards` writes them, each
  0.5 to 30 seconds and 90 seconds in all; each refusal has the `params` kind
  and names the beat and the field (`storyboard[3].secs`). A list's first beat
  names its view and opens on it with no transition, its plan says `custom`,
  and its sidecar describes the views it visits rather than naming a
  storyboard. A list's first beat also names its clock unless it sweeps a span
  rather than a number of hours, because a capture that starts wherever the
  page's clock happened to be is not reproducible, and the desktop app refuses
  that plan. A named storyboard given `view` or `at` now opens on them, with
  the page's address and the first beat agreeing, which closes issue 31:
  before, the reel opened on the requested view and then morphed away from it,
  and a client checking the address's clock checked a time the reel never
  showed. The eight names plan exactly as before, and `bluesky-video`'s size
  limit is now Bluesky's own 300 MB, where 50 MB refused a long export at high
  quality after its capture had run.
- **`render.stage` describes the stage it drew** (issue 54). Beside `svg` and
  `counts` it answers a `description` (`StageDescription`), from which the
  desktop app writes the text alternative of its geographic pane: for each
  line its two ends (one for a loop), its stations in order, where it meets
  other lines, and its branches with the station each leaves at, every name
  the one the map draws. Given the project's service day as the new optional
  `RenderStageParams.date`, each line also carries its commonest trip that day
  in whole minutes (its trips grouped by their first and last mapped calls,
  ties broken by the group with more trips, then the shorter median, then the
  names; half a minute rounds up), and `extent` is the longest of them;
  without a day both are null and no timetable is read. The minutes are cached
  per layout and day and `map.build` fills that cache, so a description after
  a draw does not read the timetable again. The cache is kept for the life of
  the process, so after a forced re-layout under the same id a day's minutes
  are those of the previous set until that day is drawn again. The SVG and the
  counts are unchanged, and `RenderStageResult.description` is required.
- **`map.build` can name a line and hide one** (issue 42, its first half).
  `map.build` takes `lines`, keyed by line label, each value an optional
  `name` (1 to 40 characters with no line break) and an optional `hidden`
  flag, described in the schema as `LineOptions`; a label the layout does not
  carry is ignored, as `colors` ignores one. A name is written where the page
  writes a line's label (its chip, its row and the time chart's band, the
  trains' titles, and the A to Z sort) through a new `data.names`, sent only
  when there is a name, so `data.lines` stays the colour lookup it was; the
  SVG writes no line text and the sidecar counts lines rather than naming
  them. A hidden line is taken off the line graph before anything reads it,
  so it has no track, no trips, no chip, row or band and no colour in either
  thumbnail; a station only it served is not drawn, one that was an
  interchange only because of it is drawn as a plain station, and the lines
  it shared track with close up over its place. The service day is still
  counted over every line, hiding every line is refused, nothing stored
  changes and `render.stage` still shows every line. With `lines` omitted the
  map, the positions file and the page's data are byte-identical.
- **An export can carry a caption, and the clock can sit in any corner**
  (issue 40). `ExportOptions` gained `caption`, 1 to 80 characters on one
  line, which the page draws as text under the title, and `clock_corner`,
  which keeps the clock bottom right by default as before, except on the reel
  and the story, where it now defaults to top right: there bottom right is
  refused (the reel's button rail, the story's bottom zone) and bottom left
  comes with a note. The safe zones became a dated table in `export.py` that
  the plan writes onto the page's address and the page draws from, so the
  preview's bottom zone is 35% where it was 22%, and on the reel and the story
  the name block, the clock and the map now sit below Instagram's top zone.
  Every other preset's address and pixels are unchanged, and `ExportOptions`
  and `CaptureJob` gained the two fields.
- **`bin/audit-page`** runs axe over an animation page in both themes, with
  `@axe-core/playwright` as the site's one new development dependency
  (issue 34).

### Changed

- **An accessibility pass over the animation page's header** (issue 34). axe
  reported nothing on the page as it stood, so the pass was done by keyboard
  and from the browser's own accessibility tree, and what it found was fixed
  in the header. Play and Pause now say what they do and announce the change
  once, politely, and the scrub reads as a time. Every control shows a 2px
  focus ring that clears 3:1 in both themes (the line chips had fallen back
  on the browser's own ring, and the view switcher's was cut off by its
  group). A line chip's dot whose colour fails 3:1 against the ground gets a
  ring in that theme, and a held key on a chip counts as one press. Under
  reduced motion the trains start paused and a view change lands at once,
  while present mode and every export are unchanged. With the pressed state
  gone from the Play button, its word is drawn muted while playing, as the
  header's other buttons are.

### Fixed

- **The page's Linear and Time views show the lines in the order `map.build`
  was given** (issue 63). They had sorted their rows A to Z whatever
  `line_order` said, so rearranging a project's lines moved nothing in either
  view, in the page or in an export's frames. A map built with an order now
  carries it to the page as `arranged` in the layout, and the Sort group gains
  a third button, "As arranged", pressed on load and not drawn when there is
  no order, on fine and coarse pointers alike, so the rows start in that order
  in both views and in every export; A to Z and Stations are as they were. A
  map built without an order writes no `arranged`, and its page data and every
  frame are unchanged by a byte (the page's numeric A to Z of "1", "2", "10"
  stays its own, not the engine's "1", "10", "2").

The schema's bytes moved (`MapBuildParams.style` and `.lines`,
`Provenance.alt`, `ExportOptions.storyboard`, `.caption` and `.clock_corner`,
`StoryboardBeat`, `CaptureJob`, `RenderStageParams.date`,
`RenderStageResult.description`), all additive at protocol 1. The Pittsburgh
snapshot's `page_without_data` was re-pinned once for the page changes of
issues 42, 40, 34 and 63; its `svg`, `data_without_lines` and `lines` did not
move.

## [0.11.0] - 2026-10-06

### Added

- **A registry entry says whether its feed publishes headways** (issue 46).
  `feeds.list` and `feeds.add` answer a new boolean, `headways`, for every
  feed: true when the operator runs its service from `frequencies.txt`
  (trains at a scheduled interval) rather than a timetable of trip times,
  which the engine had said only in prose in Mexico City's notes. Among
  the presets it is true for Mexico City alone; Phoenix carries 4
  frequency rows among 18,157 trips, and Chicago and Pittsburgh ship the
  file empty, so they stay false. For a feed a person adds it is decided
  where the zip is checked: `frequencies.txt` has rows and the trips they
  name in `trips.txt` are at least half of the feed's trips; a member the
  library cannot read answers false and the feed is still added. The
  answer is written to `user-feeds.json`, so a restart does not open the
  zip again, and a record written before the field existed reads as
  false. `FeedRecord` lists the field as required.
- **`map.build` writes a small picture of the map beside the page**
  (issue 51). After the page, `map.build` writes `<key>-thumb-dark.svg`
  and `<key>-thumb-light.svg` into the same folder as `<key>.html`: the
  network alone, about 400 units wide, with no stations' names and no
  ground rectangle, in the request's own line colours, default colour and
  line order. The furniture colours (station fill and outline) are written
  as literals, resolved through `export.resolve` against the dark and
  light entries of `export.PALETTES`, so the files read correctly in an
  `<img>`, where a `var()` falls back to white. New York's pair is 61 KB
  each. `MapBuildResult.files` gained two required keys, `thumb_dark` and
  `thumb_light`. The drawing is `schematic.thumbnail.draw`, which the
  sample cities' pictures call.
- **A picture of every sample city** (issue 52). `bin/thumbnails <folder>`
  writes `<key>-dark.svg` and `<key>-light.svg` for every preset that has a
  layout stored at the pinned LOOM. It reads the stored octi stage, which is
  the graph `map.build` draws, without the schedule, the animation or a
  page, and draws each picture with `thumbnail.draw` in the feed's own
  colours and order. A README beside the files names the engine, the LOOM
  commit, the date, the count and total size, and each preset left without
  a picture and why (feed not downloaded, no layout at this LOOM, or a
  layout that would not draw, which also fails the run). A picture an
  earlier run left for a preset that now has none is removed. A feed a
  person added is left out, because it is not a sample city and its network
  is theirs. The SVGs carry no date, so a second run writes the same bytes.
  The desktop app ships the output on its front door: the 22 presets came to
  44 files and 619 KB.
- **Two pictures of the map's themes, for the app to choose by**
  (issue 53). `bin/theme-thumbnails <folder>` writes `theme-warm-dark.svg`
  and `theme-sepia.svg`, and a README saying which engine made them and
  when, so the desktop app can offer its Warm dark and Sepia themes as two
  small pictures. Each is the same unlabelled drawing of an invented
  network of four lines and twelve stations on a 16 by 10 grid, drawn
  directly by the renderer from positions already on the grid rather than
  through LOOM, so it needs no Docker or feed and writes the same bytes on
  every run; resolved through its theme's palette and padded to 16:10
  without cropping. The ground is the palette's own `bg` (`#15120f` and
  `#f7efe1`), which is what the viewer and every export show, not the page
  card's `--map-bg`.
- **The page's seam can change the theme** (issue 29).
  `window.__present.setTheme(name)` takes `warm-dark` or `sepia`, sets or
  removes `data-theme` as the boot script does, answers false and changes
  nothing for any other name, and never writes storage (`rc-theme` stays
  the site's script's key); `state()` gains `theme`. Nothing calls it on
  load, so an export's `?theme=` still wins at boot and a captured frame is
  the one it was. The desktop app can restyle a shown map without
  reloading it, which threw away the clock, the view and the scrub.

### Changed

- **`feeds.remove` is a job, and a cancel reaches it** (issue 35). It was
  a plain handler on the server's one reader thread, so while it deleted a
  feed's zips and layouts nothing else was read: `feeds.list` queued behind
  it, and a `$/cancelRequest` for it was not read until it had returned
  and was then ignored. It now runs like `feeds.add`, so the reader keeps
  answering while it works. The write of `user-feeds.json` is its point of
  no return, and that write is now atomic (beside itself under a name of
  the writer's own, then moved into place, with the scratch file removed if
  it fails). A cancel before it leaves the feed registered with every file
  in place and is answered with the cancelled error. A cancel after it is
  not honoured, since the feed is already forgotten: the files are removed
  to the end, in name order, and the answer carries
  `"cancel_too_late": true`. The answer for the normal case is still
  exactly `{"ok": true}`; the result is now `FeedsRemoveResult`, which adds
  that one optional field. Removing a feed also removes the folders the
  native backend unpacked it into (`<key>.normalized` and its scratch),
  which every laid-out feed used to leave behind in the home; an add of the
  same key while a removal is still deleting now waits for it rather than
  losing its new zip; and a key that is not a key is refused by the library
  as the server already refused it.
- **The recorder records with Playwright's full Chromium, not the headless
  shell** (issue 21). Bare `launch()` starts a stripped build whose
  rasteriser is not the full browser's: at one version they disagreed on
  every frame of one capture by up to 99 of 255 levels on glyph and stroke
  edges. `bin/_record.js` now names `channel: "chromium"`, the same kind
  of build a viewer's browser and the desktop app's own capture run, so
  every raster the recorder makes (a still, a video, a GIF, a safe-area
  preview) differs from one made before this release, and
  `npx playwright install chromium` is the install, never `--only-shell`.
  The full browser also requests the page's icon links, which under
  `file://` are not there; the recorder no longer counts that as the
  page's failure. A launch failure is printed without the browser's path.
- **Four presets fetch their feed over https** (issue 17): New York City
  Subway and the Long Island Rail Road from the MTA's own bucket, where
  the old addresses redirected, and Miami and DART over the same paths as
  before. A test holds every preset to https unless a test-side list names
  it as plain http with a reason. The layout id hashes the feed's bytes,
  not its address, so no stored layout moves; the sidecar's `source` and
  the atlas's "Source feed" link say the new address for new exports and
  renders, and the atlas needs a rebuild to show it.
- **The site's service days are pinned** (issue 25).
  `site/src/_data/service-days.json` records the day each published map
  was drawn for, seeded with the 22 published days; a rebuild never moves
  a day, `bin/build-site --redate <key>` chooses one network's day again,
  and a stored day the feed's calendar no longer covers is refused with
  the sentence that says so rather than drawn as a page with no trips.
- **The page's four view icons are Phosphor's** (issue 19). The switcher
  draws Phosphor's regular `map-trifold`, `graph`, `line-segments` and
  `clock`, copied unmodified from the 2.1.1 release of
  `@phosphor-icons/core` under the MIT licence, with its `LICENSE` beside
  them in `page/icons/`; the site's masks, its Framework and App credits
  and the README's Licence section follow. The four Esri Calcite icons
  they replace were redistributed under a Master License Agreement whose
  current terms forbid combining them in a manner that would subject them
  to GPL-style terms, which this repository's licence is. `graph` needs no
  turn, so the schematic button's 90 degree CSS rotation is gone. The
  page's bytes changed by the icon data, one rule and two comments,
  nothing an export's frames read.

### Fixed

- **A kept still is delivered in the preset's format** (issue 30). At
  draft and high quality the engine kept the captured file as it was, so a
  PNG capture for a JPEG preset was copied whole under a `.jpg` name and
  a JPEG for a PNG preset the other way. `export.encode` now reads the
  file's first bytes: a still already in the preset's format is copied,
  any other is transcoded by ffmpeg at the preset's quality, and a file
  that is no image fails rather than being copied.

The schema's bytes moved (`FeedRecord.headways`, `MapBuildResult.files`,
`FeedsRemoveResult`), all additive at protocol 1; the desktop app's pin
takes them with its fingerprint.

## [0.10.1] - 2026-09-29

### Fixed

- **A feed's address leaves the engine without its secrets** (issue 32). A
  feed a person adds can come from a keyed link (`...?api_key=...`), and
  the engine wrote that address whole into three places a person may later
  share: the sentence of a failed download, the traceback logged with it,
  and the `.json` written beside every export, whose `source` is the
  feed's address. Text that leaves the engine now names a person's feed by
  scheme, host, path and the names of its query's parameters, with the
  user information, every query value and the fragment replaced by
  `<redacted>` (`feeds.shown`, the marker and the rule the desktop app's
  own redaction uses, so a line redacted here is unchanged by it). A
  failed download says why in the engine's own words ("the server answered
  403 Forbidden", "the server could not be reached") rather than in
  `requests`' text, which repeats the address, and carries no cause, so
  the logged traceback has nothing to print it from. A feed with no agency
  name is named from the file in the address's path, never from its query.
  A preset's address is public and is written as the registry has it. The
  record still holds a person's address whole, since that is what a later
  fetch asks for, and `feeds.list` answers it whole. Not covered: a token
  carried as a path segment, which nothing marks as a secret; and the
  debug level (`SCHEMATIC_LOG=debug`), where the protocol library prints
  each request as it arrived. No method or shape changed and the schema's
  bytes did not move.

## [0.10.0] - 2026-09-28

### Added

- **A preset's first download reports its bytes and honours a cancel**
  (E36). A preset's zip is fetched the first time something needs it --
  inside `graph.build`, `feeds.inspect`, `feeds.service` or `map.build` --
  and that download reported nothing and could not be stopped: a cancel was
  honoured only once it had finished, and the zip was kept. Inside any long
  request it now reports `job/progress` with stage `download` when it
  happens, once per chunk, with the fraction of the download's own bytes
  and the sentence `feeds.add` sends ("downloaded 65,536 of 1,732,403
  bytes"), and a cancel
  between chunks or after the last one answers the cancelled error
  (-32800) and caches nothing: no zip and no `.part`, so the next request
  downloads afresh; a request that waited for another's cancelled download
  of the same feed asks its own cancel before fetching. A feed already on
  disk reports no download. The
  protocol's description of `job/progress` and `feeds.service` says so; no
  method or shape changed, so this is additive at protocol 1. The download
  is watched per request thread (`feeds.watched`), as a LOOM tool is
  (`loom.cancellable`), rather than threaded through every caller.

## [0.9.1] - 2026-09-28

### Changed

- **A layout read from the store says only that** (E37). Its log line was
  `layout 1a2b3c4d: read from the store; nothing was laid out`, true of the
  request that read it and false of the run a client shows: the desktop
  app lays out and then draws, two requests in one log, so the draw's line
  followed the layout's four stage lines and seemed to deny them. It is
  `layout 1a2b3c4d: read from the store` now, still with how long the
  request waited when another was building it.

## [0.9.0] - 2026-09-28

### Added

- **A long request logs a line of its own for every stage** (E37). A
  request's `job/log` lines were the LOOM tools' stderr and nothing else,
  and the native tools write nothing when they succeed, so a `graph.build`
  or `map.build` that went well sent no log at all, and the desktop app's
  log panel was empty after every good run. Each stage now logs what it made
  and how long it took, in stage order. The four LOOM stages give the
  graph's summary (`gtfs2graph: 114 nodes (114 stations, 0 junctions), 112
  edges, lines: A, B, C, D, E, K (12.2 s)`), timed from after the feed is
  normalised to before the output is read back; `schedule`, `render` and
  `animate` give the sentence their progress already carries; `write` names
  its three files (`<key>.svg, <key>.html and <key>.positions.json`) where
  its progress message names the folder. A layout read from the store
  rather than made says so in one line instead (`layout 1a2b3c4d: read from
  the store; nothing was laid out`), with how long the request waited when
  another was building it. A tool's stderr still arrives beside them, and no
  line of the engine's own names a path. Level `info`, no schema change, and
  progress messages are unchanged; outside a request (the command line, the
  site) nothing is logged and no stage is read back for a line. A layout
  found in the store is now reported outside the process's layout lock.

## [0.8.3] - 2026-09-12

### Fixed

- **A page is the same bytes whatever the machine's locale** (E19). Every
  text file the engine reads or writes -- the animation page and its
  template, the SVG, the positions file, a layout's stored stages and
  `.meta.json`, an export's sidecar, the site's data -- named no encoding,
  so it took the locale's. On Windows, where the desktop app's Python
  predates the UTF-8 default, the title's em dash went out as the single
  cp1252 byte 0x97 into a page declaring UTF-8, a station name outside the
  code page raised instead of being written, and every newline became CRLF;
  with the C locale and UTF-8 mode off, `import schematic.animate` failed on
  `page.html`'s own bytes. They are all UTF-8 with LF line endings now, and
  so are the tables pandas rewrites into a feed's normalised copy, which
  took the platform's line separator. `python -m schematic.serve --schema`
  writes its bytes past the console's text layer, which on Windows turned
  each newline into CRLF and so moved the hash the desktop app pins; and the
  export's recorder output is read as UTF-8 and echoed in the console's own
  codec with a replacement character, so a line outside the code page no
  longer raises after a capture that succeeded, whether it is read or
  printed (ffprobe's output is read as UTF-8 too). A page, and the
  schema, generated on a UTF-8 machine are byte-identical to before.
- **The service day is written without `%-d`** (E19), a glibc and BSD
  extension the Windows C runtime refuses with a `ValueError`, so a build
  there failed at the schedule stage. The weekday and month now come from
  fixed English names rather than `%A` and `%B`, so the page's header, an
  export's title and the command line's summary no longer change language
  with `LC_TIME`. Under an English locale every one reads exactly as before.

## [0.8.2] - 2026-09-12

### Fixed

- **A line order no longer drops the lines it leaves out.** `line_order` was
  documented as the stacking on shared track with "the rest follow", and was
  a whitelist instead: `render` drew the labels it named and no others, and
  `linear.build` laid out the same, so an order naming two lines of six drew
  two and lost four -- on the map, in the page's rows and in the time chart
  alike. It is a preference again, as it says: the lines it names first, in
  that order, then everything else in the order it would have had anyway. A
  label the layout does not carry is ignored, as an override for one is, and
  a label named twice is drawn once. A build with no order is byte-identical
  to before.

## [0.8.1] - 2026-09-12

### Fixed

- **A cancelled `feeds.add` keeps no feed** (E23). The cancel was asked only
  between the download's chunks, so one that arrived after the last chunk --
  or at any point of an add from a file, which asks nothing while it copies
  -- fell through to the commit: the request answered with the cancelled
  error while the feed sat in the registry and its zip in the cache. It is
  asked once more before anything is kept, and the staging file goes with it.
- **A feed's own colours cannot become markup** (E24). `route_color` reached
  the SVG's `stroke` attribute exactly as the agency published it, on the
  same line as the label beside it, which was escaped -- so a colour of
  `"><script>` closed the attribute and opened an element of the feed's
  choosing, in the desktop app, in the animation page and on every map the
  site publishes. The feed's colour is now held to the same six hex digits a
  caller's override has always been held to, falling back to the default the
  way a missing colour already did, and every colour is escaped on its way
  into an attribute regardless. `feeds.inspect` reports `color` and
  `text_color` as six upper-case hex digits without the hash, or null, rather
  than whatever the column held. The line chips in the animation page are
  built from DOM nodes instead of `innerHTML`, so a line *named* with a tag
  cannot run either. A map drawn from a well-formed feed is byte-identical.

## [0.8.0] - 2026-09-11

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
