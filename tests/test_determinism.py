"""An export renders the same pixels twice.

The whole capture design rests on this: the page's clock is stepped by hand,
one ``advance(1/fps)`` per captured frame, rather than recorded in real time.
Nothing else in the suite exercises that loop -- ``test_export.py`` is pure
Python by design, and the storyboard tests only read the beats.

Deliberately *pixel* comparison, not a digest. What is reproducible is the
content; the PNG container is not always identical between runs, and a hash
would fail on a difference nobody can see. See "Compare pixels, not hashes" in
CLAUDE.md.

This covers the *video* path only. A still is captured after the page has been
left running in real time for `settle` milliseconds, so its clock lands wherever
wall-time put it and two stills of the same preset genuinely differ -- see
"A still is not reproducible" in CLAUDE.md.
"""

import shutil
from pathlib import Path

import pytest

from schematic import export, feeds
from schematic.export import Beat

# Skia does not rasterise a frame identically every time: a handful of pixels
# on an antialiased edge land a single level apart between runs. That is not
# what this test is looking for. A train drawn one place along its track, or a
# morph caught a frame early, moves a channel by tens -- comparing against a
# threshold tells the two apart, where exact equality would just be flaky.
# Measured: identical content differs by 1, a nondeterministic clock by 30-130.
DRIFT = 8

PAGE = Path(export.__file__).parent / "page" / "page.html"
BUILT = feeds.REPO_ROOT / "site" / "src" / "maps" / "la-metro-rail.html"
PLAYWRIGHT = feeds.REPO_ROOT / "site" / "node_modules" / "playwright"

needs_browser = pytest.mark.skipif(
    not (BUILT.exists() and PLAYWRIGHT.exists() and shutil.which("node")),
    reason="needs a built map page and site/node_modules/playwright")


def test_capture_cancels_the_queued_frame():
    """The one line the whole thing hangs on, guarded without a browser.

    Without the cancel, one already-queued real-time frame lands at an
    unpredictable moment after capture starts and the two runs diverge.
    """
    page = PAGE.read_text()
    body = page[page.index("setCapture(on)"):]
    assert "cancelAnimationFrame" in body[:body.index("},")], \
        "setCapture no longer cancels the queued frame; captures will drift"


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
