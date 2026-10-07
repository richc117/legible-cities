"""The thumbnail pair of every sample city, for the desktop app to ship (issue 52).

The desktop app's front door shows a small picture of each sample city. The
cards are drawn from the registry alone, with nothing downloaded, and the app
draws no map of its own, so the pictures are the engine's: ``bin/thumbnails
<folder>`` writes ``<key>-dark.svg`` and ``<key>-light.svg`` for every preset,
and a ``README.md`` saying how they were made, and the app vendors the files
(ADR-047 of the app, app issue 287).

Which cities
------------

The presets, ``feeds.FEEDS``, and not ``feeds.all()``: a feed a person added
is theirs and is no sample city, and a picture of it would ship a person's
network inside an installer. A preset is drawn when ``pipeline.stored`` finds
the layout its feed on disk names at the pinned LOOM, and is otherwise left
out and named in the README with the reason: its feed is not downloaded, or it
is downloaded and has no layout at this LOOM. Nothing is downloaded and
nothing is laid out here, since a layout made for a picture would be one the
person did not ask for (ADR-023); a preset whose source no longer answers is
simply one of the first kind. A layout that is stored and will not draw (an
empty graph) is the third, and the only one that fails the run.

How a picture is drawn
----------------------

From the stored layout's ``octi`` stage, read directly: the file is read, an
empty graph is refused with the sentence about modes (``require_edges``), and
the graph is reprojected to Web Mercator. Those are the three steps
``pipeline.run`` takes to make the graph it hands to ``thumbnail.write`` from
``map.build``, so this is the same graph a project's own thumbnail is drawn
from. What ``run`` does besides -- the schedule, the animation, the page -- a
picture does not need, and a sentence from the schedule ("feed has neither
calendar.txt nor calendar_dates.txt") would have left a card without a picture
that draws fine. The pictures come from ``thumbnail.draw`` with no overrides,
which is to say in the feed's own colours and the engine's own line order, with
the furniture resolved to literals for each palette: so a sample city's picture
is the bytes a project on the same layout and with nothing chosen would be
given.

Nothing in an SVG says when it was made, so a second run writes the same
bytes; the date is in the README. A picture an earlier run wrote for a preset
that has none now is removed, so that the folder and the README's list agree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, feeds, loom, pipeline, thumbnail
from .crs import to_mercator
from .linegraph import LineGraph

NOT_DOWNLOADED = "not downloaded"
NO_LAYOUT = "no layout at this LOOM"


@dataclass
class Report:
    """What a run did: the pictures it wrote, their size, and the presets it
    left without one."""

    # The SVGs, not the README: the README carries these counts, and a file
    # cannot say how large it is.
    written: list[Path] = field(default_factory=list)
    size: int = 0
    # Key -> why it has no picture, in the registry's order.
    missing: dict[str, str] = field(default_factory=dict)
    # The keys of ``missing`` that were laid out but could not be drawn: the
    # one reason that is a fault and not a fact about the machine.
    failed: set[str] = field(default_factory=set)


def pictures_name(key: str, theme: str) -> str:
    """``<key>-dark.svg``: not ``thumbnail``'s ``<key>-thumb-dark.svg``, which
    sits beside a project's page. These sit together, by city, in the app's
    ``samples`` folder, where the key is all the name needs to say."""
    return f"{key}-{theme}.svg"


def readme(report: Report, today: dt.date) -> str:
    """What sits beside the pictures: which engine and LOOM drew them and
    when, how many and how large, and the presets with none. The date is here
    and not in the SVGs, whose bytes then stay the same from run to run."""
    pin = loom.commit() or "not recorded (the host did not name it)"
    if report.missing:
        left = "\n".join(f"- `{key}`: {why}" for key, why in report.missing.items())
        none = ("## No picture\n\nThese presets have none, so the app shows an empty "
                f"area:\n\n{left}\n")
    else:
        none = "## No picture\n\nNone: every preset has one.\n"
    return f"""# Sample city thumbnails

A small picture of every sample city's map, once in each of the interface's
two palettes, for the desktop app's front door. The app never draws a map, so
these are the engine's.

- Engine: {__version__}
- LOOM commit: {pin}
- Made on: {today.isoformat()}
- Files: {len(report.written)}
- Total size: {report.size} bytes (the pictures; this file is not counted)

Each is `<key>-dark.svg` or `<key>-light.svg`. A picture is the network alone,
about 400 units wide: no labels, no ground, in the feed's own line colours and
the engine's own line order, with every colour a literal so that it holds up
inside an `<img>`. It is drawn from the layout the feed names at this LOOM, so
it is a picture of that layout and not a layout: a later engine, LOOM or feed
leaves it showing the earlier map until the script is run again.

{none}
## Making them again

From a checkout of the engine at the tag the app pins, with the sample feeds
downloaded and laid out at the LOOM it pins (`bin/run-all` does both):

    bin/thumbnails <folder>

It reads the stored layouts and makes none, and it writes these files into the
folder, which it creates if it is missing. It removes a picture an earlier run
left there for a preset that now has none, so the folder says what this file
says. The SVGs carry no date, so a second run over the same layouts changes the
date in this file and nothing else.
"""


def write(folder: Path, today: dt.date | None = None) -> Report:
    """Write every preset's pictures that can be drawn, and the README, into
    ``folder``, which is created if it is missing."""
    report = Report()
    folder.mkdir(parents=True, exist_ok=True)
    for key in feeds.FEEDS:
        layout = pipeline.stored(key)
        if layout is None:
            report.missing[key] = (NO_LAYOUT if feeds.get(key).zip_path.is_file()
                                   else NOT_DOWNLOADED)
            continue
        try:
            graph_ll = LineGraph.from_geojson(layout.paths["octi"])
            pipeline.require_edges(layout.feed, graph_ll)
            drawn = thumbnail.draw(graph_ll.reproject(to_mercator))
        except ValueError as exc:
            # The engine's own sentence for a person, which names no path: it
            # goes into a file that is committed elsewhere. An OSError is not
            # caught, because its text names the file.
            report.missing[key] = f"could not be drawn: {' '.join(str(exc).split())}"
            report.failed.add(key)
            continue
        for theme in thumbnail.THEMES:
            path = folder / pictures_name(key, theme)
            path.write_text(drawn[theme], encoding="utf-8", newline="\n")
            report.written.append(path)
    # A picture an earlier run left for a preset that has none now would
    # contradict the README's list of them.
    for key in report.missing:
        for theme in thumbnail.THEMES:
            (folder / pictures_name(key, theme)).unlink(missing_ok=True)
    report.size = sum(path.stat().st_size for path in report.written)
    (folder / "README.md").write_text(readme(report, today or dt.date.today()),
                                      encoding="utf-8", newline="\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bin/thumbnails",
        description="Write <key>-dark.svg and <key>-light.svg for every preset "
                    "with a stored layout, and a README, into a folder.")
    parser.add_argument("folder", type=Path,
                        help="where to write them; created if it is missing")
    args = parser.parse_args(argv)
    try:
        report = write(args.folder)
    except (OSError, ValueError) as exc:
        print(f"bin/thumbnails: {exc}", file=sys.stderr)
        return 1
    print(f"{len(report.written)} files, {report.size} bytes")
    for key, why in report.missing.items():
        print(f"no picture: {key} ({why})")
    # A fact about the machine (a feed not downloaded) is a result; a layout
    # that would not draw is a fault, and a run that hides it is not a pass.
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
