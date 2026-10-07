"""An export renders the same pixels twice.

The whole capture design rests on this: the page's clock is stepped by hand,
one ``advance(1/fps)`` per captured frame, rather than recorded in real time.
Nothing else in the suite exercises that loop -- ``test_export.py`` is pure
Python by design, and the storyboard tests only read the beats.

Deliberately *pixel* comparison, not a digest. What is reproducible is the
content; the PNG container is not always identical between runs, and a hash
would fail on a difference nobody can see. See "Compare pixels, not hashes" in
CLAUDE.md.

The still path used to be the exception: the page was left running in real
time for `settle` milliseconds before the shot, so its clock landed wherever
wall-time put it. Since the export split (E10) the recorder stops the clock
before that wait in both modes, and the guard below keeps it there.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from schematic import config, export
from schematic.export import Beat

# Skia does not rasterise a frame identically every time: a handful of pixels
# on an antialiased edge land a single level apart between runs. That is not
# what this test is looking for. A train drawn one place along its track, or a
# morph caught a frame early, moves a channel by tens -- comparing against a
# threshold tells the two apart, where exact equality would just be flaky.
# Measured: identical content differs by 1, a nondeterministic clock by 30-130.
DRIFT = 8

PAGE = Path(export.__file__).parent / "page" / "page.html"
PRESENT_JS = PAGE.parent / "present.js"
BUILT = config.REPO_ROOT / "site" / "src" / "maps" / "la-metro-rail.html"
PLAYWRIGHT = config.REPO_ROOT / "site" / "node_modules" / "playwright"

needs_browser = pytest.mark.skipif(
    not (BUILT.exists() and PLAYWRIGHT.exists() and shutil.which("node")),
    reason="needs a built map page and site/node_modules/playwright")
needs_node = pytest.mark.skipif(not shutil.which("node"), reason="needs node")


def test_capture_cancels_the_queued_frame():
    """The one line the whole thing hangs on, guarded without a browser.

    Without the cancel, one already-queued real-time frame lands at an
    unpredictable moment after capture starts and the two runs diverge.
    """
    page = PAGE.read_text()
    body = page[page.index("setCapture(on)"):]
    assert "cancelAnimationFrame" in body[:body.index("},")], \
        "setCapture no longer cancels the queued frame; captures will drift"


def test_the_recorder_stops_the_clock_before_it_waits():
    """`setCapture(true)` before the settle wait, stills included. Without it a
    still's trains land wherever wall-time put them: six exports of one preset
    once gave five distinct images."""
    js = (config.REPO_ROOT / "bin" / "_record.js").read_text()
    stop = js.index("__present.setCapture(true)")
    pin = js.index("__present.seek(s), job.at")
    wait = js.index("waitForTimeout(job.settle")
    assert stop < pin < wait, "the recorder waits with the page's clock running, or unpinned"


def test_the_recorder_launches_the_full_chromium():
    """`channel: "chromium"` is load-bearing (issue 21): bare `launch()` starts
    the headless shell, whose rasteriser differs from the full browser's by up
    to 99 of 255 levels on glyph and stroke edges. Two runs of the shell agree
    with each other, so the pixel test cannot notice a change back."""
    js = (config.REPO_ROOT / "bin" / "_record.js").read_text()
    assert 'chromium.launch({ channel: "chromium" })' in js, \
        "the recorder no longer names the full Chromium; captures will differ from the app's"


def _seam_method(page: str, name: str) -> str:
    """The text of one method of the window.__present object, from its
    signature to the `},` that closes it at the object's own indent."""
    start = page.index(f"    {name}(")
    return page[start:page.index("\n    },", start) + len("\n    },")]


def test_the_seam_has_a_theme_method_and_state_reports_the_theme():
    """`window.__present.setTheme` exists, and `state()` says which theme is
    showing -- so a client can change the theme without navigating the page
    again, which throws away the clock, the view, the scrub and the toggles."""
    page = PAGE.read_text(encoding="utf-8")
    seam = page[page.index("window.__present = {"):]
    assert "\n    setTheme(name) {" in seam, "the seam lost its theme method"
    state = seam[seam.index("    state() {"):]
    assert "theme:" in state[:state.index("\n    },")], "state() no longer reports the theme"


def test_nothing_calls_setTheme_on_load_and_nothing_writes_the_theme_key():
    """The method does nothing until called: present.js, which runs the load
    sequence from the address, never names it, and the page has no write to
    storage at all -- rc-theme is the site's script's key, and an export's
    ?theme= must still win at boot, so a captured frame is the one it was."""
    page = PAGE.read_text(encoding="utf-8")
    assert page.count("setTheme(") == 1, "setTheme is gone, or something besides the seam names it"
    assert "setTheme" not in PRESENT_JS.read_text(encoding="utf-8")
    assert "setItem" not in page, "the page writes storage; rc-theme belongs to the site's script"


@needs_node
def test_setTheme_sets_data_theme_as_the_boot_script_does_and_refuses_the_rest():
    """The method itself, run over a stand-in document element: sepia sets
    data-theme, warm-dark removes it (the page's default is the absence of the
    attribute, as in the boot script and the site's theme.js), a name the page
    does not know returns false and changes nothing, and nothing reads or
    writes storage. No browser: the method touches only `document`."""
    method = _seam_method(PAGE.read_text(encoding="utf-8"), "setTheme")
    script = """
      const attrs = new Map();
      globalThis.document = { documentElement: {
        setAttribute(k, v) { attrs.set(k, v); },
        removeAttribute(k) { attrs.delete(k); },
        getAttribute(k) { return attrs.has(k) ? attrs.get(k) : null; },
      } };
      const trap = () => { throw new Error("storage touched"); };
      globalThis.localStorage = { getItem: trap, setItem: trap, removeItem: trap };
      const seam = {""" + method + """};
      const out = [];
      const names = ["sepia", "bogus", undefined, "Sepia", "warm-dark", "warm-dark", "sepia"];
      for (const name of names) {
        const ret = seam.setTheme(name);
        out.push([name === undefined ? "undefined" : name, ret, attrs.get("data-theme") ?? null]);
      }
      console.log(JSON.stringify(out));
    """
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        ["sepia", True, "sepia"],
        ["bogus", False, "sepia"],        # refused: the theme did not move
        ["undefined", False, "sepia"],
        ["Sepia", False, "sepia"],        # names are the page's own, exactly
        ["warm-dark", True, None],        # the default is no attribute at all
        ["warm-dark", True, None],
        ["sepia", True, "sepia"],
    ]


@needs_browser
def test_two_captures_of_the_same_beats_agree_pixel_for_pixel(tmp_path):
    """Two runs, a view morph between them, compared frame by frame.

    The morph is the point: a beat that changes view is where a stray real-time
    frame would show up, because it is the only time the geometry is moving.
    """
    from PIL import Image, ImageChops

    # Small and short on purpose -- this runs in the default suite, and the
    # property does not need a large canvas to hold or fail.
    beats = (Beat(1.0, view="geographic", at="07:00", speed=120, tween=0),
             Beat(1.0, view="map", tween=0.8))
    url = export.url_for("la-metro-rail", export.PRESETS["linkedin-gif"],
                         view="map", labels=False, title=False, clock=False)
    job = {"url": url, "width": 320, "height": 320, "scale": 1, "fps": 12,
           "format": "png", "mode": "video", "beats": export.beat_payload(beats)}

    runs = []
    for name in ("first", "second"):
        out = tmp_path / name
        out.mkdir()
        export._run_recorder({**job, "frames": str(out)})
        runs.append(sorted(out.glob("*.png")))

    first, second = runs
    assert first, "the recorder wrote no frames"
    assert len(first) == len(second), "the two runs captured different frame counts"

    for a, b in zip(first, second):
        # RGB, not RGBA. getbbox() on a difference carrying an alpha band
        # reports the bounds of non-zero *alpha*, which is all zero for two
        # opaque frames -- so an RGBA comparison passes on any colour
        # difference whatsoever and asserts nothing at all.
        diff = ImageChops.difference(Image.open(a).convert("RGB"),
                                     Image.open(b).convert("RGB"))
        worst = max(band[1] for band in diff.getextrema())
        assert worst <= DRIFT, (
            f"{a.name} differs between runs by {worst}/255 per channel, "
            f"which is content, not rasterising")
