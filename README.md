# OpenSchematicMaps

Exploration into creating schematic maps using open source solutions.

Generates a **schematic transit map** (a tube-map-style SVG) and an
**interactive animation of trains running the day's timetable**, from nothing
but a city's public GTFS feed.

![LA Metro Rail](docs/la-metro-rail.png)

The pipeline is city-agnostic. LA Metro rail is the worked example; BART and
TriMet MAX run through the same code with only a registry entry added.

## How it works

[LOOM](https://github.com/ad-freiburg/loom) (University of Freiburg, GPL-3.0)
does the hard part — turning a geographic network into an octilinear one:

```
GTFS zip → gtfs2graph → topo → loom → octi → schematic line graph
```

`topo` merges overlapping track, `loom` solves the left-to-right ordering of
lines on each edge, and `octi` snaps the whole network to a 45° grid. Every
stage speaks the same GeoJSON line-graph format, so each can be inspected on its
own.

From there this repo takes over:

- **`render.py`** draws the SVG, using LOOM's solved line ordering to offset
  parallel tracks, and giving every (line, edge) a stable element id.
- **`labels.py`** places station names, rotating them off the line and testing
  collisions with oriented boxes.
- **`schedule.py`** resolves a service day and maps GTFS stops onto map nodes.
- **`animate.py`** routes each trip across the drawn network and emits a
  self-contained HTML page that runs the trains.

LOOM's own `transitmap` renderer is used as a cross-check, not as the output —
the animation needs geometry it can address by id.

OpenStreetMap is not needed for these feeds: they all ship `shapes.txt`. If a
feed has poor or missing shapes, run it through
[pfaedle](https://github.com/ad-freiburg/pfaedle) first (note that makes the
result ODbL).

## Setup

```bash
uv venv && uv pip install -e .
docker build -t openschematicmaps/loom docker/
```

The Dockerfile builds LOOM natively for your architecture with the open ILP
solvers. (Upstream's pins Ubuntu 20.04 and downloads Gurobi's linux64 tarball,
which forces amd64 emulation on Apple Silicon.)

## Use

```python
from schematic import pipeline

result = pipeline.run("la-metro-rail")
print(result.summary())
```

Writes `out/la-metro-rail.svg`, `out/animation.html` and `out/positions.json`.
Open the HTML in a browser: a clock, a time-of-day scrubber, playback speeds and
per-line toggles.

The notebooks in `notebooks/` walk the same pipeline one stage at a time, and
are the place to start if you want to see what each tool does.

```bash
pytest                                  # unit tests, plus end-to-end checks
bin/preview out/la-metro-rail.svg       # rasterise an SVG to look at it
```

## Adding a city

One entry in `FEEDS` in `src/schematic/feeds.py`:

```python
"bart": Feed(
    key="bart",
    name="BART",
    url="https://www.bart.gov/dev/schedules/google_transit.zip",
    mode="all",
    label_strip=r"-[NSEW]$",   # BART splits each line by direction
),
```

`mode` is LOOM's `-m` filter (`tram`, `subway`, `rail`, `all`, …) — for a
combined bus-and-rail feed, this is what keeps the map to rail. `label_pattern`
and `label_strip` clean up route labels; most feeds need neither.

Currently registered: `la-metro-rail`, `bart`, `trimet-max`.
