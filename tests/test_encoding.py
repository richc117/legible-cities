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
encoding gets cp1252 and a write with no newline gets CRLF, which is what
Windows does. Each child also writes a control file the bare way and the test
checks it came out wrong, so a simulation that silently stopped biting would
fail here rather than pass.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import schematic
from schematic.schedule import service_day_text

SRC = Path(schematic.__file__).parent

STATIONS = ["Łódź Fabryczna", "東京", "Zürich HB"]

# Run in a child process under a chosen locale. Writes a page, its SVG and
# its positions file the way pipeline.run does, then a control file the old,
# bare way.
CHILD = r'''
import builtins, io, json, sys
from pathlib import Path

out = Path(sys.argv[1])
simulate = sys.argv[2] == "cp1252"

if simulate:
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
(out / "page.svg").write_text(r.svg, encoding="utf-8", newline="\n")
anim = animate.build(r, graph, [], day)
animate.write(anim, r.svg, out, stem="page", name=name,
              title=f"{name} — {service_day_text(day)}",
              subtitle=f"0 trips · {service_day_text(day)}")

# The control: the bare call the engine used to make.
try:
    (out / "control.txt").write_text("—\n")
except UnicodeError:
    (out / "control.txt").write_bytes(b"refused")
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
    proc = subprocess.run(
        [sys.executable, str(script), str(out), mode, json.dumps(STATIONS)],
        env=_child_env("utf-8" if mode == "utf-8" else "c"),
        capture_output=True, timeout=120)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    return out


def test_a_page_is_byte_identical_under_c_and_cp1252_and_utf8(tmp_path):
    utf8, c, cp1252 = (_generate(tmp_path, mode) for mode in ("utf-8", "c", "cp1252"))

    # The environments really were different: the bare write is right only
    # under UTF-8. (Under the C locale ASCII refuses the dash; under cp1252
    # it is one byte and the newline is CRLF.)
    assert (utf8 / "control.txt").read_bytes() == "—\n".encode("utf-8")
    assert (c / "control.txt").read_bytes() == b"refused"
    assert (cp1252 / "control.txt").read_bytes() == b"\x97\r\n"

    for name in ("page.html", "page.positions.json", "page.svg"):
        expected = (utf8 / name).read_bytes()
        assert (c / name).read_bytes() == expected, f"{name} differs under LC_ALL=C"
        assert (cp1252 / name).read_bytes() == expected, f"{name} differs under cp1252"
        expected.decode("utf-8")  # strict: raises on a stray code-page byte
        assert b"\r\n" not in expected

    page = (utf8 / "page.html").read_bytes()
    assert "<title>Łódź — Saturday 5 September 2026</title>".encode() in page


def test_a_station_name_outside_the_code_page_round_trips(tmp_path):
    out = _generate(tmp_path, "cp1252")
    page = (out / "page.html").read_bytes().decode("utf-8")
    svg = (out / "page.svg").read_bytes().decode("utf-8")
    for station in STATIONS:
        assert station in svg, f"{station} is not drawn"
        assert station in page, f"{station} is not in the page"
    names = json.loads((out / "page.positions.json").read_bytes())["linear"]["names"]
    assert sorted(names.values()) == sorted(STATIONS)


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


def test_every_day_of_a_year_matches_the_english_names():
    """Against the numbers alone, so no locale is consulted on either side."""
    days = "Monday Tuesday Wednesday Thursday Friday Saturday Sunday".split()
    months = ("January February March April May June July August September "
              "October November December").split()
    day = dt.date(2024, 1, 1)
    while day.year == 2024:
        assert service_day_text(day) == (
            f"{days[day.weekday()]} {day.day} {months[day.month - 1]} {day.year}")
        day += dt.timedelta(days=1)


def test_no_strftime_extension_or_bare_text_io_is_left_in_the_package():
    """Guarded as text, because the failures it is about only happen on
    another platform: ``%-d`` raises on Windows, and a text open without an
    encoding takes whatever the locale says."""
    for path in sorted(SRC.glob("*.py")):
        code = [line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.lstrip().startswith("#")]
        text = "\n".join(code)
        assert not re.search(r"%-[A-Za-z]", text), f"{path.name} uses a %- directive"
        for call in re.finditer(r"\.(read_text|write_text)\(", text):
            args, depth, i = [], 1, call.end()
            while depth:
                ch = text[i]
                depth += ch == "("
                depth -= ch == ")"
                args.append(ch)
                i += 1
            assert "encoding=" in "".join(args), (
                f"{path.name}: {call.group(1)} without an encoding near "
                f"{text[call.start() - 40:call.end()]!r}")
