"""A GIF's colour table, composed from the plan (issue 33; the desktop app's
ADR-055).

``palettegen`` counted the table from the frames, so noise inside the
tolerance of 8 moved nearly every entry and changed pixels two captures
agreed on. The table is now a function of the theme and the map's line
colours alone: the ground, the text, the muted text, a ramp between the
first two, and per distinct line colour the colour, its steps toward the
ground and its midpoint with the text. These hold its arithmetic.
"""

import datetime as dt
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from schematic import config, export, serve
from test_lines import KEY as STAND_KEY, stand  # noqa: F401  (the store and the feed stood in)
from test_serve import Client, check

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


# ---------------------------------------------------- the encode's guarantees
#
# ADR-055's, on a synthetic map instead of a capture: the theme's ground, its
# title, muted subtitle and clock, BART's seven colours as octilinear lines,
# three of them sharing a corridor edge to edge, stations with their names,
# and trains with their halos, moving. Drawn at four times the size and
# reduced, so every edge is anti-aliased as a browser's is. Three folders
# per theme: S, a near twin within 8 of it, and S with one box in one frame.

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

KEY = "la-metro-rail"
PRESET = "linkedin-gif"                    # 640 x 640, the smallest GIF preset
SIZE = 640
FRAMES = 24
SUPER = 4
BOX = {"frame": FRAMES // 2, "x": 300, "y": 500, "size": 10, "color": (255, 0, 0)}

MAP_LINES = [
    ("#ff0000", [(40, 150), (200, 150), (320, 270), (320, 600)]),
    ("#ffff33", [(40, 158), (196, 158), (312, 274), (312, 600)]),
    ("#339933", [(40, 166), (192, 166), (304, 278), (304, 470), (420, 586), (600, 586)]),
    ("#0099cc", [(600, 300), (420, 300), (328, 392), (328, 600)]),
    ("#ff9933", [(600, 308), (424, 308), (336, 396), (336, 520), (430, 614)]),
    ("#b0bec7", [(100, 420), (296, 420), (296, 300), (380, 216), (600, 216)]),
    ("#000000", [(60, 520), (200, 520), (280, 440), (288, 440)]),
]
MAP_STATIONS = [(40, 158), (120, 158), (200, 154), (260, 214), (316, 300), (316, 380),
                (316, 460), (316, 560), (600, 304), (500, 304), (420, 304), (332, 440),
                (360, 586), (500, 586), (100, 420), (200, 420), (296, 360), (440, 216),
                (540, 216), (60, 520), (140, 520), (240, 480)]


def _along(points: list, t: float) -> tuple:
    """The point a fraction ``t`` of the way along a polyline, wrapping."""
    lengths = [((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5 for a, b in zip(points, points[1:])]
    left = (t % 1.0) * sum(lengths)
    for (a, b), length in zip(zip(points, points[1:]), lengths):
        if left <= length:
            f = left / length
            return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f)
        left -= length
    return points[-1]


def _map_frame(theme: str, i: int):
    from PIL import Image, ImageDraw, ImageFont
    ground, text = export.PALETTES[theme]["bg"], export.PALETTES[theme]["label"]
    muted = export.MUTED[theme]
    im = Image.new("RGB", (SIZE * SUPER, SIZE * SUPER), ground)
    draw = ImageDraw.Draw(im)

    def font(size):
        return ImageFont.load_default(size=size * SUPER)

    def at(x, y):
        return (x * SUPER, y * SUPER)

    def disc(x, y, r, **kw):
        draw.ellipse([(x - r) * SUPER, (y - r) * SUPER, (x + r) * SUPER, (y + r) * SUPER], **kw)

    for color, points in MAP_LINES:
        draw.line([at(*p) for p in points], fill=color, width=6 * SUPER, joint="curve")
    for x, y in MAP_STATIONS:
        disc(x, y, 4.5, fill=text, outline=ground, width=6)
    for k, (x, y) in enumerate(MAP_STATIONS):
        draw.text(at(x + 8, y - 14), f"Station {k + 1}", fill=text, font=font(10))
    draw.text(at(24, 24), "Bay Area Rapid Transit", fill=text, font=font(22))
    draw.text(at(24, 54), "Saturday 5 September 2026", fill=muted, font=font(13))
    minute = 7 * 60 + 10 + i
    draw.text(at(520, 600), f"{minute // 60:02d}:{minute % 60:02d}", fill=text, font=font(18))
    for j, (color, points) in enumerate(MAP_LINES):
        for start in (0.0, 0.5):
            x, y = _along(points, start + i / FRAMES * 0.3 + j * 0.07)
            disc(x, y, 5.0, fill=ground)
            disc(x, y, 3.5, fill=color)
    return im.reduce(SUPER)


def _save(frames, into: Path) -> Path:
    from PIL import Image
    into.mkdir(parents=True)
    for i, frame in enumerate(frames):
        Image.fromarray(frame, "RGB").save(into / f"{i:06d}.png", compress_level=1)
    return into


def _near_twin(frames, theme: str):
    """Within 8 of ``frames`` by construction: on 2% of each frame's pixels,
    edges first (a pixel whose neighbour differs by more than 24), every
    channel moved by 1 to 8 either way, seeded."""
    import numpy as np
    out = frames.copy()
    touched = round(0.02 * SIZE * SIZE)
    for i, frame in enumerate(frames):
        rng = np.random.default_rng([33, ["dark", "light"].index(theme), i])
        a = frame.astype(np.int16)
        edge = np.zeros(frame.shape[:2], bool)
        dx = np.abs(a[:, 1:] - a[:, :-1]).max(2) > 24
        dy = np.abs(a[1:] - a[:-1]).max(2) > 24
        edge[:, 1:] |= dx
        edge[:, :-1] |= dx
        edge[1:] |= dy
        edge[:-1] |= dy
        edges, rest = np.flatnonzero(edge), np.flatnonzero(~edge)
        if len(edges) >= touched:
            pick = rng.choice(edges, touched, replace=False)
        else:
            pick = np.concatenate([edges, rng.choice(rest, touched - len(edges), replace=False)])
        move = rng.integers(1, 9, size=(touched, 3)) * rng.choice([-1, 1], size=(touched, 3))
        flat = a.reshape(-1, 3)
        flat[pick] = np.clip(flat[pick] + move, 0, 255)
        out[i] = flat.reshape(frame.shape).astype(np.uint8)
    return out


def _decode(gif: Path):
    import numpy as np
    done = subprocess.run([shutil.which("ffmpeg"), "-loglevel", "error", "-i", str(gif),
                           "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                          capture_output=True, timeout=300)
    assert done.returncode == 0, done.stderr.decode()
    return np.frombuffer(done.stdout, np.uint8).reshape(-1, SIZE, SIZE, 3)


def _colour_table(gif: Path) -> bytes:
    """The GIF's global colour table, and the check that no frame has its own."""
    data = gif.read_bytes()
    assert data[:6] == b"GIF89a" and data[10] & 0x80
    end = 13 + 3 * 2 ** ((data[10] & 7) + 1)
    table, pos = data[13:end], end
    while data[pos] != 0x3B:
        if data[pos] == 0x21:                       # an extension: skip its sub-blocks
            pos += 2
        else:                                       # an image: no local table
            assert data[pos] == 0x2C and not data[pos + 9] & 0x80
            pos += 11
        while data[pos]:
            pos += data[pos] + 1
        pos += 1
    return table


def _gif(theme: str, frames: Path, dest: Path, colors=BART) -> Path:
    """The GIF ``export.encode`` makes of ``frames`` for a plan carrying
    ``colors``."""
    job = export.plan(KEY, PRESET, theme=theme, quality="draft")
    job = replace(job, colors=None if colors is None else tuple(colors))
    return export.encode(job, frames, dest / job.filename)[0]


@pytest.fixture(scope="module", params=["dark", "light"])
def pair(request, tmp_path_factory):
    """Per theme: S, its near twin and its one-box copy, each as frames, as
    the GIF the encode makes of them and as that GIF decoded."""
    import numpy as np
    theme = request.param
    work = tmp_path_factory.mktemp(f"gif-{theme}")
    source = np.stack([np.asarray(_map_frame(theme, i)) for i in range(FRAMES)])
    boxed = source.copy()
    b = BOX
    boxed[b["frame"], b["y"]:b["y"] + b["size"], b["x"]:b["x"] + b["size"]] = b["color"]
    out = {"theme": theme, "work": work}
    for name, frames in (("S", source), ("twin", _near_twin(source, theme)), ("box", boxed)):
        folder = _save(frames, work / name)
        gif = _gif(theme, folder, work / f"gif-{name}")
        out[name] = {"frames": frames, "folder": folder, "gif": gif, "decoded": _decode(gif)}
    return out


@needs_ffmpeg
def test_identical_frames_encode_to_identical_gifs(pair, tmp_path):
    again = _gif(pair["theme"], pair["S"]["folder"], tmp_path / "again")
    copy = shutil.copytree(pair["S"]["folder"], tmp_path / "copy")
    elsewhere = _gif(pair["theme"], copy, tmp_path / "elsewhere")
    assert again.read_bytes() == elsewhere.read_bytes() == pair["S"]["gif"].read_bytes()


@needs_ffmpeg
def test_frames_within_8_share_the_colour_table(pair):
    import numpy as np
    moved = np.abs(pair["twin"]["frames"].astype(np.int16) - pair["S"]["frames"])
    assert moved.max() == 8
    assert all(moved[i].any() for i in range(FRAMES)), "every frame of the twin differs"
    assert _colour_table(pair["twin"]["gif"]) == _colour_table(pair["S"]["gif"])


@needs_ffmpeg
def test_no_pixel_the_inputs_agree_on_differs_out(pair):
    differ_in = (pair["S"]["frames"] != pair["twin"]["frames"]).any(axis=3)
    differ_out = (pair["S"]["decoded"] != pair["twin"]["decoded"]).any(axis=3)
    assert not (differ_out & ~differ_in).any(), "a pixel the inputs agree on moved"
    assert differ_out.sum() <= differ_in.sum()


@needs_ffmpeg
def test_one_box_changes_the_box_alone(pair):
    differ = (pair["S"]["decoded"] != pair["box"]["decoded"]).any(axis=3)
    b = BOX
    inside = differ[b["frame"], b["y"]:b["y"] + b["size"], b["x"]:b["x"] + b["size"]].sum()
    assert inside > 0, "the box is in the GIF"
    assert differ.sum() == inside, "nothing outside the box moved"
    assert _colour_table(pair["box"]["gif"]) == _colour_table(pair["S"]["gif"])


@needs_ffmpeg
def test_the_gif_is_faithful_to_its_frames(pair):
    """ADR-055's bounds, mean absolute error per channel at most 1.5 and
    the worst frame's at most 2.0, measured on 0 to 255."""
    import numpy as np
    error = np.abs(pair["S"]["decoded"].astype(np.int16) - pair["S"]["frames"])
    mean, worst = error.mean(), error.mean(axis=(1, 2, 3)).max()
    print(f"\n{pair['theme']}: mean {mean:.3f}, worst frame {worst:.3f}, "
          f"{pair['S']['gif'].stat().st_size:,} bytes")
    assert mean <= 1.5
    assert worst <= 2.0


@needs_ffmpeg
def test_the_palette_is_the_plans_whatever_the_frames_hold(pair, tmp_path, monkeypatch):
    """The same plan and colours give the same palette bytes and the same
    colour table for frames that share nothing, here seeded noise; the
    table is the composed one, in whatever order the GIF keeps it."""
    import numpy as np
    handed = []
    run = export._ffmpeg

    def spy(args, **kw):
        handed.extend(Path(a).read_bytes() for a in args if a.endswith("palette.png"))
        return run(args, **kw)

    monkeypatch.setattr(export, "_ffmpeg", spy)
    noise = np.random.default_rng(55).integers(0, 256, size=(4, SIZE, SIZE, 3), dtype=np.uint8)
    gifs = [_gif(pair["theme"], pair["S"]["folder"], tmp_path / "map"),
            _gif(pair["theme"], _save(noise, tmp_path / "noise"), tmp_path / "noise-gif")]
    table = export.gif_palette(pair["theme"], BART)
    assert handed == [export.palette_png(table)] * 2
    assert _colour_table(gifs[0]) == _colour_table(gifs[1])
    entries = _colour_table(gifs[1])
    assert sorted(entries[i:i + 3] for i in range(0, 768, 3)) == sorted(bytes(c) for c in table)


@needs_ffmpeg
@pytest.mark.parametrize("colors,counted", [
    (GRID[:60], export.COUNTED),           # past the table's last bound
    (GRID, export.COUNTED),
    (None, export.COUNTED_BEFORE),         # colours not known: as before issue 33
    (GRID[:59], None),                     # composed: ffmpeg never counts
    (BART, None),
])
def test_past_59_colours_the_palette_is_counted_from_the_frames(colors, counted, tmp_path,
                                                                 monkeypatch):
    import numpy as np
    calls = []
    run = export._ffmpeg

    def spy(args, **kw):
        calls.append(args)
        return run(args, **kw)

    monkeypatch.setattr(export, "_ffmpeg", spy)
    frames = _save([np.asarray(_map_frame("dark", 0))], tmp_path / "frames")
    _gif("dark", frames, tmp_path / "out", colors=colors)
    counting = [a for a in calls if any(arg.startswith("palettegen") for arg in a)]
    assert [a[a.index("-vf") + 1] for a in counting] == ([counted] if counted else [])
    assert len(calls) == len(counting) + 1
    assert calls[-1][calls[-1].index("-lavfi") + 1].endswith(
        "[s][1:v]paletteuse=dither=bayer:bayer_scale=3")


# ------------------------------------------------- where the colours come from
#
# The plan carries them: from the folder map.build wrote the page into, which
# the desktop app names with ``out`` (its project's id), else beside the
# site's page. The encode composes the palette from the plan alone.

APP_PAGE = "app://local/projects/p1/la-metro-rail.html"
SORTED = ("#000000", "#0099cc", "#339933", "#b0bec7", "#ff0000", "#ff9933", "#ffff33")


def _positions(folder: Path, lines, key: str = KEY) -> Path:
    """A positions file as ``animate.write`` leaves one, with ``lines``."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{key}.positions.json"
    path.write_text(json.dumps({"date": "2026-09-05", "lines": lines, "paths": []}),
                    encoding="utf-8")
    return path


def _link(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:                       # Windows without the privilege
        pytest.skip(f"cannot make a symbolic link here: {exc}")


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A scratch engine home, whose out/ holds the pages map.build wrote."""
    root = tmp_path / "home"
    (root / "out").mkdir(parents=True)
    monkeypatch.setenv(config.ENV, str(root))
    return root


def test_a_pages_colours_are_its_positions_files_lines_distinct_and_sorted(tmp_path):
    _positions(tmp_path, {**dict(zip("ABCDEFGH", BART)), "Z": "#FF0000"})
    assert export.page_colors(tmp_path, KEY) == SORTED
    assert export.page_colors(tmp_path, "boston-t") is None


@pytest.mark.parametrize("data", [
    b'{"paths": []}', b'{"lines": ["#ff0000"]}', b'{"lines": {"A": "red"}}',
    b'{"lines": {"A": 7}}', b'["#ff0000"]', b'{"lines": {"A": "#ff0000"}', b"",
    b'{"lines": {"A": "#ff0000"}}\xff',                 # not UTF-8
    b'{"lines": {"A": "#ff0000"}, "casings": "#ffffff"}',
    b'{"lines": {"A": "#ff0000"}, "casings": ["white"]}',
])
def test_a_positions_file_that_cannot_be_read_as_one_gives_no_colours(tmp_path, data):
    (tmp_path / f"{KEY}.positions.json").write_bytes(data)
    assert export.page_colors(tmp_path, KEY) is None


def test_a_gif_plan_carries_the_colours_of_the_page_out_names(home):
    _positions(home / "out" / "p1", dict(zip("ABCDEFGH", BART)))
    job = export.plan(KEY, PRESET, page=APP_PAGE, out="p1")
    assert job.colors == SORTED
    answer = {**job.to_dict(), "filename": job.filename}
    assert answer["colors"] == list(SORTED)
    check(answer, "CaptureJob")


@pytest.mark.parametrize("folder", ["missing", "empty", "other-key"])
def test_a_plan_whose_folder_holds_no_page_carries_none(home, folder):
    (home / "out" / "empty").mkdir()
    _positions(home / "out" / "other-key", {"A": "#ff0000"}, key="bart")
    job = export.plan(KEY, PRESET, page=APP_PAGE, out=folder)
    assert job.colors is None and job.to_dict()["colors"] is None
    check({**job.to_dict(), "filename": job.filename}, "CaptureJob")


@pytest.mark.parametrize("preset", ["linkedin-video", "instagram-reel", "linkedin", "bluesky"])
def test_only_a_gif_plan_carries_colours(home, preset):
    _positions(home / "out" / "p1", {"A": "#ff0000"})
    assert export.plan(KEY, preset, page=APP_PAGE, out="p1").colors is None


def test_without_out_the_colours_are_the_site_pages_or_none(home, tmp_path, monkeypatch):
    maps = tmp_path / "maps"
    _positions(maps, {"A": "#0099CC", "B": "#ff0000"})
    _positions(home / "out", {"A": "#00ff00"})           # out/ itself is never read
    monkeypatch.setattr(export, "MAPS_DIR", maps)
    assert export.plan(KEY, PRESET).colors == ("#0099cc", "#ff0000")
    assert export.plan(KEY, PRESET, page=APP_PAGE).colors is None
    # out wins over the site's page, whatever page says.
    _positions(home / "out" / "p1", {"A": "#123456"})
    assert export.plan(KEY, PRESET, out="p1").colors == ("#123456",)


@pytest.mark.parametrize("out", ["../x", "..", ".", "", "/x", "a/b", "x/", "../out/p1"])
def test_out_is_one_folder_name_never_a_path(home, out):
    """Even where the path would reach a real page: beside the home, and the
    project's own folder reached the long way round."""
    _positions(home / "x", {"A": "#ff0000"})
    _positions(home / "out" / "p1", {"A": "#ff0000"})
    with pytest.raises(ValueError, match="out must be one folder name"):
        export.plan(KEY, PRESET, page=APP_PAGE, out=out)


def test_out_as_an_absolute_path_to_a_page_is_refused(home):
    folder = home / "out" / "p1"
    _positions(folder, {"A": "#ff0000"})
    with pytest.raises(ValueError, match="out must be one folder name"):
        export.plan(KEY, PRESET, page=APP_PAGE, out=str(folder))


def test_a_positions_file_that_is_a_link_outside_its_folder_is_refused(home, tmp_path):
    outside = _positions(tmp_path / "elsewhere", {"A": "#ff0000"})
    (home / "out" / "p1").mkdir()
    _link(home / "out" / "p1" / f"{KEY}.positions.json", outside)
    with pytest.raises(ValueError, match="link to outside its folder"):
        export.plan(KEY, PRESET, page=APP_PAGE, out="p1")
    # A link that stays in the folder is the folder's own file.
    inside = _positions(home / "out" / "p2", {"A": "#ff0000"}, key="kept")
    _link(home / "out" / "p2" / f"{KEY}.positions.json", inside)
    assert export.plan(KEY, PRESET, page=APP_PAGE, out="p2").colors == ("#ff0000",)
    # A dangling link is no page.
    _link(home / "out" / "p3", tmp_path / "nowhere")
    (home / "out" / "p4").mkdir()
    _link(home / "out" / "p4" / f"{KEY}.positions.json", tmp_path / "nowhere.json")
    assert export.plan(KEY, PRESET, page=APP_PAGE, out="p4").colors is None


def test_a_project_folder_that_is_a_link_outside_is_refused(home, tmp_path):
    _positions(tmp_path / "elsewhere", {"A": "#ff0000"})
    _link(home / "out" / "p1", tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="leads outside the engine's out folder"):
        export.plan(KEY, PRESET, page=APP_PAGE, out="p1")


# ------------------------------------------------------------------ casings
#
# A line's casing (issue 55) is drawn under it in its own colour, so its
# pixels are the map's as much as the line's are. The positions file names
# the casings' colours only where a line has one, and the plan counts them
# with the lines' colours, towards k and the cap at 59.

STAND_DAY = dt.date(2026, 9, 10)
# sha256 of the stood-in map's positions file before casings were written,
# taken at the commit before this test: uncased, and widened but uncased.
UNCASED = {
    "plain": "d9dda941f83720585520c5e9611dba5afad06c6f39303e853536c354bc6e76dc",
    "wide": "c72ef588933e6b122017ea8879e251af20c9427470f51c8b0e58040130312abc",
}


@pytest.mark.parametrize("name,lines", [("plain", None), ("wide", {"A": {"width": 1.5}})])
def test_an_uncased_maps_data_is_byte_for_byte_what_it_was(stand, tmp_path, name, lines):  # noqa: F811
    import hashlib
    out = tmp_path / name
    stand.run(out, date=STAND_DAY, **({"lines": lines} if lines else {}))
    data = (out / f"{STAND_KEY}.positions.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == UNCASED[name]
    assert b'"casings"' not in data


def test_a_cased_maps_plan_carries_its_casing_colour(stand, home):  # noqa: F811
    """Through the real map build, stood in: two lines cased in one colour,
    written once, which the plan counts with the lines' colours and the
    palette composes its steps from."""
    folder = home / "out" / "p1"
    _result, svg, data, _page = stand.run(folder, date=STAND_DAY, lines={
        "A": {"casing": {"width": 0.5, "color": "#F0E0C0"}},
        "B": {"width": 1.5, "casing": {"width": 0.25, "color": "#f0e0c0"}}})
    assert 'stroke="#F0E0C0"' in svg and 'stroke="#f0e0c0"' in svg     # drawn, both
    assert data["casings"] == ["#f0e0c0"]
    job = export.plan(STAND_KEY, PRESET, page=APP_PAGE, out="p1")
    assert job.colors == tuple(sorted({*(c.lower() for c in data["lines"].values()), "#f0e0c0"}))
    table = export.gif_palette("dark", job.colors)
    k = 6                                       # a handful of colours
    ground, text = (21, 18, 15), (242, 237, 230)
    cased = (240, 224, 192)
    assert cased in table
    assert all(export._mix(cased, ground, j / k) in table for j in range(1, k))
    assert export._mix(cased, text, 0.5) in table


def test_a_casing_in_a_lines_colour_is_one_colour(tmp_path):
    (tmp_path / f"{KEY}.positions.json").write_text(json.dumps(
        {"lines": {"A": "#ff0000"}, "casings": ["#FF0000", "#00ff00"]}), encoding="utf-8")
    assert export.page_colors(tmp_path, KEY) == ("#00ff00", "#ff0000")


def test_the_cap_at_59_counts_the_casings(home):
    folder = home / "out" / "p1"
    folder.mkdir()
    path = folder / f"{KEY}.positions.json"
    path.write_text(json.dumps({"lines": dict(enumerate(GRID[:55])), "casings": GRID[55:60]}),
                    encoding="utf-8")
    job = export.plan(KEY, PRESET, page=APP_PAGE, out="p1")
    assert len(job.colors) == 60 and export.gif_palette("dark", job.colors) is None
    path.write_text(json.dumps({"lines": dict(enumerate(GRID[:55])), "casings": GRID[:4]}),
                    encoding="utf-8")
    job = export.plan(KEY, PRESET, page=APP_PAGE, out="p1")
    assert len(job.colors) == 55 and export.gif_palette("dark", job.colors) is not None


# --------------------------------------------------------- through the protocol


@pytest.fixture
def client():
    c = Client()
    yield c
    c.endpoint.close()


@needs_ffmpeg
def test_the_colours_ride_the_plan_from_export_plan_to_export_encode(home, client, tmp_path):
    """The app names its project's folder, and hands the plan back as it came."""
    import numpy as np
    _positions(home / "out" / "p1", dict(zip("ABCDEFGH", BART)))
    asked = {"key": KEY, "preset": PRESET, "page": APP_PAGE, "out": "p1",
             "options": {"quality": "draft"}}
    check(asked, "ExportPlanParams")
    plan = client.call("export.plan", asked)["result"]
    check(plan, "CaptureJob")
    assert plan["colors"] == list(SORTED)
    assert serve._capture_job(plan).colors == SORTED
    frames = _save([np.asarray(_map_frame("dark", i)) for i in range(3)], tmp_path / "frames")
    # The page's colours change after the plan: the encode reads the plan's.
    _positions(home / "out" / "p1", {"A": "#123456"})
    dest = tmp_path / "out" / plan["filename"]
    done = client.call("export.encode", {"plan": plan, "source": str(frames),
                                         "dest": str(dest)})
    assert "result" in done, done
    entries = _colour_table(dest)
    table = export.gif_palette("dark", BART)
    assert sorted(entries[i:i + 3] for i in range(0, 768, 3)) == sorted(bytes(c) for c in table)


@pytest.mark.parametrize("out", ["../x", "/x", "a/b", "a\\b", ".hidden", "x\n"])
def test_the_protocol_refuses_an_out_that_is_not_a_folder_name(home, client, out):
    error = client.call("export.plan", {"key": KEY, "preset": PRESET, "out": out})["error"]
    assert error["code"] == -32602 and error["data"]["kind"] == "params", error
    assert error["data"]["hint"].startswith("out must be a folder name")


def test_the_protocol_refuses_a_positions_file_that_leaves_its_folder(home, client, tmp_path):
    outside = _positions(tmp_path / "elsewhere", {"A": "#ff0000"})
    (home / "out" / "p1").mkdir()
    _link(home / "out" / "p1" / f"{KEY}.positions.json", outside)
    error = client.call("export.plan", {"key": KEY, "preset": PRESET, "page": APP_PAGE,
                                        "out": "p1"})["error"]
    assert error["code"] == -32000 and error["data"]["kind"] == "export", error
    assert "link to outside its folder" in error["data"]["hint"]
    assert str(tmp_path) not in error["data"]["hint"]


def test_a_plan_handed_back_without_colours_reads_as_none(home, client):
    _positions(home / "out" / "p1", {"A": "#ff0000"})
    plan = client.call("export.plan", {"key": KEY, "preset": PRESET, "page": APP_PAGE,
                                       "out": "p1"})["result"]
    older = {k: v for k, v in plan.items() if k != "colors"}
    assert serve._capture_job(older).colors is None
    assert serve._capture_job({**plan, "colors": None}).colors is None


# ----------------------------------------------------------- the old path, kept


@needs_ffmpeg
def test_a_plan_without_colours_encodes_byte_for_byte_as_before(tmp_path, monkeypatch):
    """No colours, as from a client that names no folder: the two passes the
    encode always ran, palettegen=stats_mode=diff then paletteuse, give the
    same bytes run by hand."""
    import numpy as np
    frames = _save([np.asarray(_map_frame("light", i)) for i in range(4)], tmp_path / "frames")
    job = export.plan(KEY, PRESET, theme="light", page=APP_PAGE, quality="draft")
    assert job.colors is None
    made = export.encode(job, frames, tmp_path / "made" / job.filename)[0]
    ffmpeg, src = shutil.which("ffmpeg"), str(frames / "%06d.png")
    palette, by_hand = tmp_path / "palette.png", tmp_path / "by-hand.gif"
    for args in (["-framerate", "12", "-i", src, "-vf", "palettegen=stats_mode=diff",
                  str(palette)],
                 ["-framerate", "12", "-i", src, "-i", str(palette), "-lavfi",
                  "scale=640:640:flags=lanczos[s];[s][1:v]paletteuse=dither=bayer:bayer_scale=3",
                  str(by_hand)]):
        subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-nostats", *args], check=True,
                       timeout=300)
    assert made.read_bytes() == by_hand.read_bytes()


@needs_ffmpeg
def test_the_encode_never_reads_a_page(tmp_path, monkeypatch):
    """The palette is the plan's: the encode composes it from the plan's
    colours and asks no folder for any."""
    import numpy as np
    frames = _save([np.asarray(_map_frame("dark", 0))], tmp_path / "frames")
    job = replace(export.plan(KEY, PRESET, page=APP_PAGE, quality="draft"), colors=SORTED)
    monkeypatch.setattr(export, "page_colors", lambda *a: pytest.fail("the encode read a page"))
    monkeypatch.setattr(export, "out_folder", lambda *a: pytest.fail("the encode read a page"))
    gif = export.encode(job, frames, tmp_path / "out" / job.filename)[0]
    entries = _colour_table(gif)
    table = export.gif_palette("dark", BART)
    assert sorted(entries[i:i + 3] for i in range(0, 768, 3)) == sorted(bytes(c) for c in table)


@needs_ffmpeg
def test_the_sites_export_composes_from_its_pages_colours(tmp_path, monkeypatch):
    """``run`` plans for the site's page, so the plan carries the colours of
    the positions file beside that page."""
    maps = tmp_path / "maps"
    _positions(maps, {"A": "#0099cc", "B": "#ff0000"})
    monkeypatch.setattr(export, "MAPS_DIR", maps)

    def capture(job, *, frames=None, out=None):
        import numpy as np
        assert job.colors == ("#0099cc", "#ff0000")
        return _save([np.asarray(_map_frame("dark", i)) for i in range(3)], frames)

    monkeypatch.setattr(export, "capture", capture)
    written = export.run(KEY, PRESET, out=tmp_path / "out")
    table = export.gif_palette("dark", ["#0099cc", "#ff0000"])
    entries = _colour_table(written[0])
    assert sorted(entries[i:i + 3] for i in range(0, 768, 3)) == sorted(bytes(c) for c in table)
