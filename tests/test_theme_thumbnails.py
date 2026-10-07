"""The two pictures of the map's themes the desktop app chooses by (issue 53).

``bin/theme-thumbnails <folder>`` draws an invented four-line network, with
no labels, in the viewer's two palettes. The app ships the files in its style
cell, so what it needs of them is a contract: plain SVG that an ``<img>`` can
show, a ground that is the palette's own (what the viewer and every export
paint), the same drawing in both, the route colours untouched, and bytes that
do not move from run to run.

None of this needs LOOM, Docker or ``data/``: the network is built in code.
"""

import datetime as dt
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from schematic import __version__, theme_thumbnails
from schematic.export import PALETTES

ROOT = Path(__file__).resolve().parent.parent
SVG = "{http://www.w3.org/2000/svg}"

# The files, the palette each is resolved with, and its ground: the literals
# the viewer paints (PALETTES' ``bg``), not the page card's ``--map-bg``.
GROUNDS = {"theme-warm-dark.svg": ("dark", "#15120f"),
           "theme-sepia.svg": ("light", "#f7efe1")}

# The design research's route colours, each 3.46:1 or better (to two places)
# on both grounds.
ROUTES = {"A": "#d6322f", "B": "#2f6fd6", "C": "#1e9150", "D": "#c2690f"}


@pytest.fixture(scope="module")
def made(tmp_path_factory) -> dict[str, str]:
    """What the script's own writer puts in a folder, read back from disk."""
    folder = tmp_path_factory.mktemp("thumbnails")
    theme_thumbnails.write(folder, dt.date(2026, 10, 6))
    return {name: (folder / name).read_text(encoding="utf-8") for name in GROUNDS}


def view_box(root: ET.Element) -> tuple[float, float, float, float]:
    x, y, w, h = (float(v) for v in root.get("viewBox").split())
    return x, y, w, h


def margins(root: ET.Element, inner: str) -> tuple[float, float, float, float]:
    """Left, top, right, bottom gaps between the viewBox and the box ``inner``."""
    x, y, w, h = view_box(root)
    ix, iy, iw, ih = (float(v) for v in inner.split())
    return ix - x, iy - y, (x + w) - (ix + iw), (y + h) - (iy + ih)


# ------------------------------------------------------------------ plain SVG

@pytest.mark.parametrize("name", GROUNDS)
def test_each_file_is_plain_svg_with_no_script_variable_or_text(made, name):
    text = made[name]
    root = ET.fromstring(text)           # well-formed, with its comment first
    assert root.tag == f"{SVG}svg"
    for forbidden in ("<script", "var(", "<text"):
        assert forbidden not in text, forbidden


# --------------------------------------------------------------------- ground

@pytest.mark.parametrize("name", GROUNDS)
def test_the_first_drawn_element_is_the_palettes_ground_over_the_whole_view(made, name):
    theme, ground = GROUNDS[name]
    assert PALETTES[theme]["bg"] == ground
    root = ET.fromstring(made[name])
    first = next(el for el in root
                 if el.tag not in {f"{SVG}title", f"{SVG}desc", f"{SVG}metadata"})
    assert first.tag == f"{SVG}rect"
    x, y, w, h = view_box(root)
    assert [float(first.get(a)) for a in ("x", "y", "width", "height")] == \
        pytest.approx([x, y, w, h], abs=0.005)
    assert first.get("fill") == ground


# ---------------------------------------------------------------------- shape

# The network the research chose, written out again here on purpose: the
# fixture's own table is what a change to it would edit.
STATIONS = {(1, 5), (5, 5), (8, 5), (11, 5), (15, 5),     # A
            (5, 9), (5, 1),                                # B, besides (5, 5)
            (8, 8), (14, 2),                               # C, besides (11, 5)
            (11, 9), (11, 3), (11, 1)}                     # D, besides (11, 5)
INTERCHANGES = {(5, 5), (11, 5)}


def drawn_at(x: int, y: int) -> tuple[str, str]:
    """Where grid point (x, y) is drawn: 32 units a step, x from 1, y down from 9."""
    return f"{(x - 1) * 32:.2f}", f"{(9 - y) * 32:.2f}"


def test_the_fixture_is_twelve_stations_on_eleven_runs():
    graph = theme_thumbnails.fixture()
    assert len(graph.stations) == 12 and len(graph.edges) == 11
    assert {(int(n.coord[0]), int(n.coord[1])) for n in graph.stations} == STATIONS


@pytest.mark.parametrize("name", GROUNDS)
def test_each_file_draws_two_interchanges_and_ten_stations_where_the_grid_puts_them(made, name):
    circles = [(c.get("cx"), c.get("cy"), c.get("r"))
               for c in ET.fromstring(made[name]).iter(f"{SVG}circle")]
    interchanges = [(cx, cy) for cx, cy, r in circles if r == "6.00"]
    stations = [(cx, cy) for cx, cy, r in circles if r == "4.20"]
    assert sorted(interchanges) == sorted(drawn_at(*p) for p in INTERCHANGES)
    assert ("128.00", "128.00") in interchanges and ("320.00", "128.00") in interchanges
    assert sorted(stations) == sorted(drawn_at(*p) for p in STATIONS - INTERCHANGES)
    assert len(stations) == 10 and len(circles) == 12


# -------------------------------------------------------------------- framing

@pytest.mark.parametrize("name", GROUNDS)
def test_the_view_is_16_by_10_and_holds_the_whole_network_evenly(made, name):
    root = ET.fromstring(made[name])
    x, y, w, h = view_box(root)
    assert w / h == pytest.approx(16 / 10, abs=0.001)

    # The renderer's own box for the network without labels, then what is
    # drawn at the furthest: no station or line is cut to make the shape.
    left, top, right, bottom = margins(root, root.get("data-viewbox-nolabels"))
    assert min(left, top, right, bottom) >= -0.005
    for circle in root.iter(f"{SVG}circle"):
        cx, cy, r = (float(circle.get(a)) for a in ("cx", "cy", "r"))
        assert x <= cx - r and cx + r <= x + w
        assert y <= cy - r and cy + r <= y + h

    # Padded evenly on whichever side was short: the two gaps agree to
    # within what two decimals of rounding allow.
    assert left == pytest.approx(right, abs=0.02)
    assert top == pytest.approx(bottom, abs=0.02)


def test_a_drawing_of_either_shape_is_padded_out_never_cropped():
    drawing = theme_thumbnails.draw()
    box = re.compile(r'viewBox="[^"]+"')
    # Taller than 16:10 grows its width, wider than 16:10 grows its height;
    # the middle of the old box stays the middle of the new one.
    for old, new in [("0 0 100 400", (-270.0, 0.0, 640.0, 400.0)),
                     ("0 0 800 100", (0.0, -200.0, 800.0, 500.0))]:
        framed = theme_thumbnails.frame(box.sub(f'viewBox="{old}"', drawing, count=1),
                                        "dark")
        root = ET.fromstring(framed)
        assert view_box(root) == pytest.approx(new, abs=0.005)
        ground = next(iter(root))
        assert [float(ground.get(a)) for a in ("x", "y", "width", "height")] == \
            pytest.approx(new, abs=0.005)


def test_a_drawing_whose_root_has_no_size_is_refused_not_given_the_backdrops():
    drawing = theme_thumbnails.draw()
    root = re.compile(r'(<svg\b[^>]*?\s)width="[\d.]+" height="[\d.]+"')
    bare = root.sub(r"\g<1>", drawing, count=1)
    assert bare != drawing and 'width="' in bare       # the backdrop's own stays
    with pytest.raises(ValueError, match="no longer has the size"):
        theme_thumbnails.frame(bare, "dark")


# ------------------------------------------------------------ one drawing, twice

DRAWN_COLOUR = re.compile(r'(fill|stroke)="[^"]*"')


def test_the_two_files_differ_only_in_resolved_colours_and_the_ground(made):
    dark, sepia = (made[name] for name in GROUNDS)
    assert dark != sepia
    assert DRAWN_COLOUR.sub(r'\1=""', dark) == DRAWN_COLOUR.sub(r'\1=""', sepia)


# -------------------------------------------------------------------- colours

@pytest.mark.parametrize("name", GROUNDS)
def test_the_four_route_colours_are_verbatim_and_no_other_stroke_is_drawn(made, name):
    theme, _ = GROUNDS[name]
    root = ET.fromstring(made[name])
    lines = {g.get("data-line"): g.get("stroke")
             for g in root.iter(f"{SVG}g") if g.get("class") == "line"}
    assert lines == ROUTES
    for label, colour in ROUTES.items():
        assert f'stroke="{colour}"' in made[name], label

    # Everything else that strokes is a station's ring, in the palette's own.
    others = {el.get("stroke") for el in root.iter()
              if el.get("stroke") and el.get("data-line") is None}
    assert others == {PALETTES[theme]["station-stroke"]}


def luminance(colour: str) -> float:
    rgb = [int(colour[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("label", ROUTES)
def test_each_route_colour_reads_on_both_grounds(label):
    for theme in ("dark", "light"):
        # The research wrote "at least 3.46:1", a figure rounded to two places:
        # D on Sepia is 3.4556. Held to what is true, and well over WCAG's 3
        # for a graphic.
        ratio = contrast(theme_thumbnails.LINES[label][0], PALETTES[theme]["bg"])
        assert ratio >= 3.45


def test_the_marginal_pair_is_on_the_record():
    """D on Sepia is the thinnest margin of the eight; a change to either
    colour that moves it is a decision, not a drift."""
    assert contrast(theme_thumbnails.LINES["D"][0], PALETTES["light"]["bg"]) == \
        pytest.approx(3.456, abs=0.001)


# ------------------------------------------------------------------------ size

@pytest.mark.parametrize("name", GROUNDS)
def test_each_file_is_under_20_kb(made, name):
    assert len(made[name].encode("utf-8")) < 20_000


# ------------------------------------------------------------- the script itself

@pytest.mark.skipif(sys.platform == "win32", reason="bin/ scripts are bash")
def test_the_script_writes_the_two_files_and_a_readme_and_the_same_bytes_again(tmp_path):
    def run(folder: str) -> subprocess.CompletedProcess:
        # Run from tmp_path with a relative folder: it lands where the caller
        # stands, and the folder is created.
        return subprocess.run([str(ROOT / "bin" / "theme-thumbnails"), folder],
                              cwd=tmp_path, capture_output=True, text=True, timeout=120)

    first, second = run("one"), run("two/nested")
    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    for folder in ("one", "two/nested"):
        assert sorted(p.name for p in (tmp_path / folder).iterdir()) == \
            ["README.md", "theme-sepia.svg", "theme-warm-dark.svg"]

    for name in GROUNDS:
        one = (tmp_path / "one" / name).read_bytes()
        assert one == (tmp_path / "two/nested" / name).read_bytes()
        # The date is the README's, never the SVG's, or the bytes would move
        # with the day.
        assert not re.search(rb"\d{4}-\d{2}-\d{2}", one)
        assert __version__.encode() in one

    readme = (tmp_path / "one" / "README.md").read_text(encoding="utf-8")
    assert f"engine {__version__} on {dt.date.today().isoformat()}" in readme
    assert "bin/theme-thumbnails <folder>" in readme
    assert str(tmp_path) not in readme and str(ROOT) not in readme


def test_main_writes_the_files_and_says_where(tmp_path, capsys):
    """The path the script takes, called in-process, so it is held on Windows too."""
    assert theme_thumbnails.main([str(tmp_path / "a")]) == 0
    assert sorted(p.name for p in (tmp_path / "a").iterdir()) == \
        ["README.md", "theme-sepia.svg", "theme-warm-dark.svg"]
    said = capsys.readouterr().out.split()
    assert sorted(Path(p).name for p in said) == \
        ["README.md", "theme-sepia.svg", "theme-warm-dark.svg"]


def test_main_answers_a_folder_it_cannot_make_with_one_line_and_exit_1(tmp_path, capsys):
    (tmp_path / "file").write_text("not a folder", encoding="utf-8")
    assert theme_thumbnails.main([str(tmp_path / "file")]) == 1
    seen = capsys.readouterr()
    assert seen.out == ""
    assert seen.err.startswith("bin/theme-thumbnails: ")
    assert seen.err.count("\n") == 1
