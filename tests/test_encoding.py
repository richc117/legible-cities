"""A page is the same bytes whatever the machine's locale says (E19).

The engine's text files used to be read and written with bare
``read_text``/``write_text``, which take the locale's encoding. On Windows,
where the desktop app's pinned Python predates the UTF-8 default, that wrote
the title's em dash as the single cp1252 byte 0x97 into a page declaring
``<meta charset="utf-8">``, raised on a station name outside the code page,
and turned every newline into CRLF; under ``LC_ALL=C`` with UTF-8 mode off,
``import schematic.animate`` failed outright on ``page.html``'s own bytes.

The service day was formatted with ``%-d``, which the Windows C runtime
refuses, and with ``%A``/``%B``, which follow ``LC_TIME``.

macOS and Linux have no cp1252 locale to switch to, so the Windows case is
simulated in a child process: ``open`` is wrapped so that a text open with no
encoding gets cp1252 and a write with no newline gets CRLF, ``os.linesep`` is
CRLF (pandas' ``to_csv`` reads it), and stdout is a text stream that writes
CRLF, which is what Windows does. Each child also makes the old, bare calls
into control files and the test checks they came out wrong, so a simulation
that silently stopped biting would fail here rather than pass.
"""

from __future__ import annotations

import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import schematic
from schematic import config
from schematic.schedule import service_day_text
from test_feeds import GOOD, gtfs_zip

SRC = Path(schematic.__file__).parent

STATIONS = ["Łódź Fabryczna", "東京", "Zürich HB"]

# Run in a child process under a chosen locale. Every file it keeps is one the
# engine wrote: a page and its positions file the way pipeline.run writes them,
# a user feed's record and its normalised routes table, and the protocol's
# schema as --schema prints it. Then the old, bare calls, into control files.
CHILD = r'''
import builtins, io, json, locale, os, sys, zipfile
from pathlib import Path

out = Path(sys.argv[1])
simulate = sys.argv[2] == "cp1252"
(out / "encoding.txt").write_bytes(locale.getencoding().encode("ascii"))

if simulate:
    os.linesep = "\r\n"
    _open = io.open

    def windows_open(file, mode="r", buffering=-1, encoding=None, errors=None,
                     newline=None, closefd=True, opener=None):
        if "b" not in mode:
            if encoding in (None, "locale"):
                encoding = "cp1252"
            if newline is None and any(c in mode for c in "wax+"):
                newline = "\r\n"
        return _open(file, mode, buffering, encoding, errors, newline, closefd, opener)

    io.open = builtins.open = windows_open

import datetime as dt
from schematic import animate
from schematic.crs import to_mercator
from schematic.linegraph import LineGraph
from schematic.render import render
from schematic.schedule import service_day_text

stations = json.loads(sys.argv[3])
feats = [{"type": "Feature",
          "geometry": {"type": "Point", "coordinates": [19.0 + i * 0.02, 51.7]},
          "properties": {"id": f"n{i}", "station_id": f"S{i}", "station_label": name}}
         for i, name in enumerate(stations)]
for i in range(len(stations) - 1):
    feats.append({"type": "Feature",
                  "geometry": {"type": "LineString",
                               "coordinates": [[19.0 + i * 0.02, 51.7],
                                               [19.0 + (i + 1) * 0.02, 51.7]]},
                  "properties": {"from": f"n{i}", "to": f"n{i + 1}",
                                 "lines": [{"id": "Ł1", "label": "Ł1", "color": "0072bc"}]}})
graph = LineGraph.from_geojson({"type": "FeatureCollection", "features": feats})
graph = graph.reproject(to_mercator)
name = "Łódź"
day = dt.date(2026, 9, 5)
r = render(graph, title=name)
anim = animate.build(r, graph, [], day)
animate.write(anim, r.svg, out, stem="page", name=name,
              title=f"{name} — {service_day_text(day)}",
              subtitle=f"0 trips · {service_day_text(day)}")

# A feed a person added, named outside the code page: the registry's record
# and the copy normalised for LOOM, whose routes table pandas writes.
from schematic import config, feeds
feed = feeds.add(Path(sys.argv[4]), key="lodz", name=stations[0])
(out / "user-feeds.json").write_bytes(feeds.user_file().read_bytes())
with zipfile.ZipFile(feeds.normalize(feed)) as zf:
    (out / "routes.txt").write_bytes(zf.read("routes.txt"))

# --schema, into a stdout that is a text stream the way a console's is.
from schematic import serve
real = sys.stdout
with open(out / "schema.json", "wb") as raw:
    sys.stdout = io.TextIOWrapper(raw, encoding="cp1252" if simulate else "utf-8",
                                  newline="\r\n" if simulate else None)
    try:
        serve.main(["--schema"])
        sys.stdout.flush()
    finally:
        sys.stdout.detach()
        sys.stdout = real

# The controls: the bare calls the engine used to make.
try:
    (out / "control.txt").write_text("—\n")
except UnicodeError:
    (out / "control.txt").write_bytes(b"refused")
import pandas as pd
buf = io.StringIO()
pd.DataFrame({"a": ["x"]}).to_csv(buf, index=False)
(out / "control.csv").write_bytes(buf.getvalue().encode("utf-8"))
with open(out / "control-stdout.txt", "wb") as raw:
    stream = io.TextIOWrapper(raw, encoding="utf-8", newline="\r\n" if simulate else None)
    stream.write("x\n")
    stream.flush()
    stream.detach()
'''


def _child_env(mode: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if not (k.startswith("LC_") or k in {"LANG", "LANGUAGE", "PYTHONIOENCODING",
                                                "PYTHONUTF8"})}
    if mode == "utf-8":
        env["PYTHONUTF8"] = "1"
    else:
        # PEP 540 turns UTF-8 mode on by itself under the C locale; the app's
        # interpreter is not guaranteed that, so turn it off to see the locale.
        env["LC_ALL"] = "C"
        env["PYTHONUTF8"] = "0"
    return env


def _generate(tmp_path: Path, mode: str) -> Path:
    out = tmp_path / mode
    out.mkdir()
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    source = tmp_path / "feed.zip"
    if not source.exists():
        tables = {**GOOD, "routes.txt": ("route_id,agency_id,route_short_name,route_long_name,"
                                         "route_type\nR1,M,,Linia Łódź 東京,1\n")}
        gtfs_zip(source, tables)
    env = _child_env("utf-8" if mode == "utf-8" else "c")
    env[config.ENV] = str(tmp_path / f"home-{mode}")
    proc = subprocess.run(
        [sys.executable, str(script), str(out), mode, json.dumps(STATIONS), str(source)],
        env=env, capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return out


def test_a_page_is_byte_identical_under_c_and_cp1252_and_utf8(tmp_path):
    utf8, c, cp1252 = (_generate(tmp_path, mode) for mode in ("utf-8", "c", "cp1252"))

    # The environments really were different: the bare calls are right only
    # under UTF-8. (Under an ASCII C locale the dash is refused; under cp1252
    # it is one byte and every newline is CRLF.)
    assert (utf8 / "control.txt").read_bytes() == "—\n".encode("utf-8")
    if (c / "encoding.txt").read_text(encoding="ascii").lower().replace("-", "") in {
            "ascii", "usascii", "ansi_x3.41968"}:
        assert (c / "control.txt").read_bytes() == b"refused"
    # Elsewhere (Windows, musl) the C locale is not ASCII, and the C leg is a
    # second UTF-8 or code-page run: its bytes are still held below.
    assert (cp1252 / "control.txt").read_bytes() == b"\x97\r\n"
    assert (cp1252 / "control.csv").read_bytes() == b"a\r\nx\r\n"
    assert (cp1252 / "control-stdout.txt").read_bytes() == b"x\r\n"

    for name in ("page.html", "page.positions.json", "user-feeds.json", "routes.txt",
                 "schema.json"):
        expected = (utf8 / name).read_bytes()
        assert (c / name).read_bytes() == expected, f"{name} differs under LC_ALL=C"
        assert (cp1252 / name).read_bytes() == expected, f"{name} differs under cp1252"
        expected.decode("utf-8")  # strict: raises on a stray code-page byte
        assert b"\r\n" not in expected

    page = (utf8 / "page.html").read_bytes()
    assert "<title>Łódź — Saturday 5 September 2026</title>".encode() in page
    assert "Linia Łódź 東京".encode() in (utf8 / "routes.txt").read_bytes()
    # The schema as a person's shell gets it, byte for byte.
    run = subprocess.run([sys.executable, "-m", "schematic.serve", "--schema"],
                         capture_output=True, timeout=120, check=True)
    assert (cp1252 / "schema.json").read_bytes() == run.stdout


def test_a_station_name_outside_the_code_page_round_trips(tmp_path):
    out = _generate(tmp_path, "cp1252")
    page = (out / "page.html").read_bytes().decode("utf-8")
    svg = page[page.index("<svg"):page.index("</svg>")]
    for station in STATIONS:
        assert station in svg, f"{station} is not drawn"
    names = json.loads((out / "page.positions.json").read_bytes())["linear"]["names"]
    assert sorted(names.values()) == sorted(STATIONS)
    records = json.loads((out / "user-feeds.json").read_bytes())
    assert [r["name"] for r in records] == [STATIONS[0]]


def test_the_animation_module_imports_under_the_c_locale(tmp_path):
    probe = ("import locale, sys\n"
             "print(sys.flags.utf8_mode, locale.getencoding())\n"
             "import schematic.animate\n")
    proc = subprocess.run([sys.executable, "-c", probe], env=_child_env("c"),
                          capture_output=True, timeout=120, cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    utf8_mode, encoding = proc.stdout.decode().split()
    if encoding.lower().replace("-", "") == "utf8":
        pytest.skip("this platform's C locale is UTF-8, so it proves nothing here")
    assert utf8_mode == "0"


# ------------------------------------------------------------- service day

@pytest.mark.parametrize("day,text", [
    (dt.date(2026, 9, 5), "Saturday 5 September 2026"),
    (dt.date(2026, 9, 10), "Thursday 10 September 2026"),
    (dt.date(2025, 1, 1), "Wednesday 1 January 2025"),
    (dt.date(2024, 2, 29), "Thursday 29 February 2024"),
    (dt.date(2026, 12, 31), "Thursday 31 December 2026"),
    (dt.date(2025, 6, 9), "Monday 9 June 2025"),
    (dt.date(2025, 5, 4), "Sunday 4 May 2025"),
])
def test_the_service_day_is_written_without_a_platform_extension(day, text):
    assert service_day_text(day) == text


def test_the_summary_keeps_its_two_digit_day():
    assert service_day_text(dt.date(2026, 9, 2), pad=True) == "Wednesday 02 September 2026"
    assert service_day_text(dt.date(2025, 7, 1), pad=True) == "Tuesday 01 July 2025"


def test_every_day_of_a_leap_year_reads_as_python_writes_it_in_english():
    """The oracle is ``strftime`` in this process, which is English: Python
    starts in the C locale for ``LC_TIME`` and nothing here calls
    ``setlocale``. Only the portable directives are used on this side."""
    import locale
    assert locale.setlocale(locale.LC_TIME) in {"C", "POSIX"}, "the oracle needs LC_TIME=C"
    day = dt.date(2024, 1, 1)
    seen = 0
    while day.year == 2024:
        assert service_day_text(day) == f"{day:%A} {day.day} {day:%B} {day.year}"
        assert service_day_text(day, pad=True) == f"{day:%A %d %B %Y}"
        day += dt.timedelta(days=1)
        seen += 1
    assert seen == 366


# ----------------------------------------------------------------- the guard

# ZipFile.open is binary whatever it is given; these are the names the
# package binds a ZipFile to.
_ZIP_HANDLES = {"zf", "zin", "zout"}


def _keywords(call: ast.Call) -> set[str]:
    return {k.arg for k in call.keywords if k.arg}


def _mode(call: ast.Call, position: int) -> str | None:
    for k in call.keywords:
        if k.arg == "mode":
            return k.value.value if isinstance(k.value, ast.Constant) else None
    if len(call.args) > position and isinstance(call.args[position], ast.Constant):
        return call.args[position].value
    return "r" if len(call.args) <= position else None


def _findings(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    def say(node: ast.AST, what: str) -> None:
        found.append(f"{path.name}:{node.lineno}: {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if re.search(r"%-[A-Za-z]", node.value):
                say(node, "a %- strftime directive (the Windows C runtime refuses it)")
        if not isinstance(node, ast.Call):
            continue
        func, kw = node.func, _keywords(node)
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        receiver = func.value if isinstance(func, ast.Attribute) else None
        if name == "read_text" and "encoding" not in kw:
            say(node, "read_text without encoding=")
        if name == "write_text" and not {"encoding", "newline"} <= kw:
            say(node, "write_text without both encoding= and newline=")
        if name == "open":
            if isinstance(receiver, ast.Name) and receiver.id in _ZIP_HANDLES:
                continue
            mode = _mode(node, 0 if isinstance(func, ast.Attribute) else 1)
            if mode is None:
                say(node, "open with a mode this guard cannot read")
            elif "b" not in mode and "encoding" not in kw:
                say(node, "a text-mode open without encoding=")
        if name == "to_csv" and "lineterminator" not in kw:
            say(node, "to_csv without lineterminator= (it defaults to os.linesep)")
        if (path.name == "serve.py" and name == "write"
                and isinstance(receiver, ast.Attribute) and receiver.attr == "stdout"):
            say(node, "sys.stdout.write (text mode; CRLF on Windows): write bytes to the buffer")
    return found


def test_no_strftime_extension_or_platform_shaped_text_io_is_left_in_the_package():
    """Guarded as source, because the failures it is about happen only on
    another platform: ``%-d`` raises on Windows, a text open without an
    encoding takes whatever the locale says, and a text write without a
    newline, ``to_csv`` without a terminator and a text-mode stdout all write
    CRLF there."""
    found = [f for path in sorted(SRC.glob("*.py")) for f in _findings(path)]
    assert not found, "\n".join(found)


def test_the_guard_sees_what_it_is_for(tmp_path):
    bad = tmp_path / "serve.py"
    bad.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "f\"{d:%A %-d}\"\n"
        "Path('a').read_text()\n"
        "Path('a').write_text('x', encoding='utf-8')\n"
        "open('a', 'w')\n"
        "Path('a').open()\n"
        "open('a', 'rb')\n"
        "zf.open('a')\n"
        "df.to_csv(buf, index=False)\n"
        "sys.stdout.write('x')\n"
        "sys.stdout.buffer.write(b'x')\n",
        encoding="utf-8", newline="\n")
    lines = sorted(int(f.split(":")[1]) for f in _findings(bad))
    assert lines == [3, 4, 5, 6, 7, 10, 11]
