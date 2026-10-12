"""A GIF's colour table, composed from the plan (issue 33; the desktop app's
ADR-055).

``palettegen`` counted the table from the frames, so noise inside the
tolerance of 8 moved nearly every entry and changed pixels two captures
agreed on. The table is now a function of the theme and the map's line
colours alone: the ground, the text, the muted text, a ramp between the
first two, and per distinct line colour the colour, its steps toward the
ground and its midpoint with the text. These hold its arithmetic.
"""

import io
import itertools
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from schematic import export

PAGE = Path(export.__file__).parent / "page" / "page.html"

# BART's eight lines as its page draws them: two bridge lines share black,
# so seven distinct colours.
BART = ["#339933", "#ff9933", "#000000", "#000000", "#0099cc", "#ff0000", "#ffff33", "#b0bec7"]

# Colours on a coarse grid, so none of the composed entries coincide and a
# count of the table is a count of the rule.
GRID = ["#%02x%02x%02x" % c for c in itertools.product((40, 100, 160, 220), repeat=3)]


def _distinct(table: list) -> int:
    return len(dict.fromkeys(table))


# ------------------------------------------------------------- the composition


@pytest.mark.parametrize("theme,block", [("dark", ":root {"),
                                         ("light", ':root[data-theme="sepia"] {')])
def test_the_fixed_colours_are_the_pages_ground_text_and_muted(theme, block):
    """The three colours the page's furniture paints, read from page.html,
    are the table's first three."""
    page = PAGE.read_text(encoding="utf-8")
    start = page.index(block)
    css = page[start:page.index("}", start)]
    found = {name: re.search(rf"--{name}:\s*(#[0-9a-f]{{6}})", css).group(1)
             for name in ("bg", "text", "muted")}
    assert found == {"bg": export.PALETTES[theme]["bg"],
                     "text": export.PALETTES[theme]["label"],
                     "muted": export.MUTED[theme]}
    rgb = [tuple(int(found[n][i:i + 2], 16) for i in (1, 3, 5)) for n in ("bg", "text", "muted")]
    assert export.gif_palette(theme, BART)[:3] == rgb


def test_a_line_colour_takes_itself_its_steps_to_the_ground_and_its_midpoint():
    """One red line on the dark ground, written out: the ramp's sixteen steps
    strictly between the ground and the text, then the red, five steps
    toward the ground and its midpoint with the text; the rest is ground."""
    table = export.gif_palette("dark", ["#ff0000"])
    ground = (21, 18, 15)
    assert table[:3] == [ground, (242, 237, 230), (195, 184, 170)]
    assert table[3:19] == [
        (34, 31, 28), (47, 44, 40), (60, 57, 53), (73, 70, 66), (86, 82, 78), (99, 95, 91),
        (112, 108, 104), (125, 121, 116), (138, 134, 129), (151, 147, 141), (164, 160, 154),
        (177, 173, 167), (190, 185, 179), (203, 198, 192), (216, 211, 205), (229, 224, 217)]
    assert table[19:26] == [(255, 0, 0), (216, 3, 2), (177, 6, 5), (138, 9, 8), (99, 12, 10),
                            (60, 15, 12), (248, 118, 115)]
    assert table[26:] == [ground] * 230


def test_the_light_theme_ramps_from_its_own_ground():
    table = export.gif_palette("light", ["#0099CC"])
    assert table[:3] == [(247, 239, 225), (45, 36, 29), (101, 87, 72)]
    assert table[3] == (235, 227, 213) and table[18] == (57, 48, 41)
    assert table[19:26] == [(0, 153, 204), (41, 167, 208), (82, 182, 211), (124, 196, 214),
                            (165, 210, 218), (206, 225, 222), (22, 94, 116)]
    assert table[26:] == [(247, 239, 225)] * 230


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_bart_composes_68_colours_and_188_ground(theme):
    """ADR-055's count: 3 + 16 + 7 x 7 distinct, the rest the ground."""
    table = export.gif_palette(theme, BART)
    assert len(table) == 256
    assert _distinct(table[:68]) == 68
    assert table[68:] == [table[0]] * 188


def test_the_order_is_the_colours_never_the_order_they_came_in():
    """Shuffled, repeated or in capitals, the same colours make the same
    table, and their blocks follow their RGB values."""
    table = export.gif_palette("dark", BART)
    for colors in (list(reversed(BART)), BART[3:] + BART[:3], [c.upper() for c in BART],
                   BART + BART):
        assert export.gif_palette("dark", colors) == table
    starts = [table[19 + 7 * i] for i in range(7)]
    assert starts == sorted(starts) == sorted({tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))
                                               for c in BART})


def test_the_table_is_the_same_whatever_the_hash_seed():
    """A set's order of strings changes with the hash seed; the table must
    not. Four interpreters, four seeds, one table."""
    script = ("import sys; from schematic import export; "
              "print(export.gif_palette('dark', sys.argv[1:]))")
    seen = set()
    for seed in ("0", "1", "2", "3"):
        done = subprocess.run([sys.executable, "-c", script, *GRID[:33]], capture_output=True,
                              text=True, timeout=60, env={**os.environ, "PYTHONHASHSEED": seed})
        assert done.returncode == 0, done.stderr
        seen.add(done.stdout)
    assert len(seen) == 1
    assert seen.pop().strip() == str(export.gif_palette("dark", GRID[:33]))


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("count,distinct", [
    (0, 19),                    # the furniture alone
    (1, 26),
    (33, 250),                  # k = 6: 3 + 16 + 33 x 7
    (34, 155),                  # k = 3: 3 + 16 + 34 x 4
    (59, 255),                  # the last that fit: 3 + 16 + 59 x 4
])
def test_k_is_6_up_to_33_colours_and_3_up_to_59(theme, count, distinct):
    table = export.gif_palette(theme, GRID[:count])
    assert len(table) == 256
    assert _distinct(table) == distinct
    assert table[distinct:] == [table[0]] * (256 - distinct)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_past_59_colours_there_is_no_table_to_compose(theme):
    assert export.gif_palette(theme, GRID[:60]) is None
    assert export.gif_palette(theme, GRID) is None
    # Counted as colours, not lines: sixty lines in fifty-nine colours fit.
    assert export.gif_palette(theme, GRID[:59] + GRID[:1]) is not None


@pytest.mark.parametrize("bad", ["red", "#f00", "#ff00001", "ff0000", "#gg0000", None, 0xff0000])
def test_a_colour_not_written_rrggbb_is_refused(bad):
    with pytest.raises(ValueError, match="#rrggbb"):
        export.gif_palette("dark", ["#ff0000", bad])


def test_a_theme_the_page_lacks_is_refused():
    with pytest.raises(ValueError, match="dark or light"):
        export.gif_palette("sepia", BART)


# -------------------------------------------------------------------- the PNG


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_the_png_reads_back_as_the_table_row_by_row(theme):
    from PIL import Image
    table = export.gif_palette(theme, BART)
    data = export.palette_png(table)
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        assert (im.format, im.mode, im.size) == ("PNG", "RGB", (16, 16))
        assert [im.getpixel((i % 16, i // 16)) for i in range(256)] == table


def test_the_png_is_the_same_bytes_for_the_same_table():
    a = export.palette_png(export.gif_palette("dark", BART))
    b = export.palette_png(export.gif_palette("dark", list(reversed(BART))))
    assert a == b
    assert a.startswith(b"\x89PNG\r\n\x1a\n")
    assert export.palette_png(export.gif_palette("light", BART)) != a


def test_ffmpeg_reads_the_png_as_256_pixels(tmp_path):
    """What paletteuse is handed: ffmpeg's own decoder agrees with Pillow."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg not installed")
    table = export.gif_palette("dark", GRID[:33])
    (tmp_path / "p.png").write_bytes(export.palette_png(table))
    done = subprocess.run([ffmpeg, "-loglevel", "error", "-i", str(tmp_path / "p.png"),
                           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout == bytes(v for color in table for v in color)


@pytest.mark.parametrize("table", [[(0, 0, 0)] * 255, [(0, 0, 0)] * 257, [(0, 0)] * 256,
                                   [(0, 0, 256)] * 256])
def test_a_table_that_is_not_256_rgb_colours_is_refused(table):
    with pytest.raises(ValueError):
        export.palette_png(table)


def test_making_the_png_needs_no_imaging_library():
    """Pillow is a development dependency. The engine composes and writes
    the palette with it unimportable."""
    script = ("import sys; sys.modules['PIL'] = None; from schematic import export; "
              "data = export.palette_png(export.gif_palette('light', sys.argv[1:])); "
              "assert 'PIL' not in [m.split('.')[0] for m in sys.modules if sys.modules[m]]; "
              "sys.stdout.buffer.write(data)")
    done = subprocess.run([sys.executable, "-c", script, *BART], capture_output=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr.decode()
    assert done.stdout == export.palette_png(export.gif_palette("light", BART))
