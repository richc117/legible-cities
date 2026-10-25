"""A frame counter a capture can read out of its own pixels (engine issue 85).

The desktop app wants to prove that every frame it captures was painted after
the clock step it asked for (the app's issue 100: one frame in thirteen runs
once came from the wrong paint). The page's script cannot say so, because it
is always current; only the composited image can. So ``state().draws`` counts
every ``draw()``, and under ``strip=<px>`` in present mode the page reserves a
band of that height below ``#stage`` and draws the count there in pure black
and white, which the capture reads back out of the same image before it crops
the stage. The stage, and everything drawn over it, lays out in the frame's
height alone, so the frame cropped from a capture with the strip is the frame
a capture without it takes.

The rule that holds it together: an address without the parameter, or with
one the page does not take, sets nothing and moves nothing.

The encoding, as the stylesheet documents it: ``draws`` modulo 65536 as
sixteen bits from the most significant, two blocks a bit across the whole
width (white then black for a 1, black then white for a 0), each read at its
middle.
"""

from __future__ import annotations

import base64
import io
import json
import re
import subprocess
from pathlib import Path

from PIL import Image, ImageChops

from schematic import export
from test_overlay import DATE, KEY, PLAYWRIGHT, SENTENCE, _page, needs_browser

PAGE = Path(export.__file__).parent / "page" / "page.html"
PRESENT_JS = PAGE.parent / "present.js"
BITS = 16
STRIP = 24
DRIFT = 8          # as tests/test_determinism.py has it


def _css(page: str) -> str:
    """The page's stylesheet, without its comments."""
    css = page[page.index("<style>"):page.index("</style>")]
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def read_strip(image: Image.Image, strip: int, scale: float = 1) -> int | None:
    """The number the strip at the foot of ``image`` says, or None where it
    says none: each block is read at its middle, and every pair must be one
    white block and one black."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    y = int(height - strip * scale / 2)
    value = 0
    for bit in range(BITS):
        pair = []
        for block in (2 * bit, 2 * bit + 1):
            r, g, b = rgb.getpixel((int((block + 0.5) * width / (2 * BITS)), y))
            pair.append(min(r, g, b) > 127)
        if pair[0] == pair[1]:
            return None
        value = value << 1 | pair[0]
    return value


# ------------------------------------------------------------- the page, as text


def test_without_the_strip_every_unit_is_the_viewports_own():
    """What makes an address without the strip lay out as it always did: the
    two variables are declared under ``[data-strip]`` alone, every use of them
    falls back to the very unit it replaced, and the root takes the attribute
    and ``--strip`` only inside the boot script's check."""
    page = PAGE.read_text(encoding="utf-8")
    css = _css(page)
    for name, unit in (("--vmin", "1vmin"), ("--frame-h", "100vh")):
        # Declared in one block, the strip's.
        declared = re.findall(r"([^{}]*)\{[^{}]*" + name + r"\s*:", css)
        assert [d.strip() for d in declared] == [":root[data-strip]"], (name, declared)
        # Read everywhere else with the unit it stands for as its fallback.
        fallbacks = re.findall(r"var\(" + name + r", ([^)]*)\)", css)
        assert fallbacks and set(fallbacks) == {unit}, (name, fallbacks)
    # Read without one only by the strip's own rules: --vmin's declaration and
    # the stage's height under the strip.
    bare = [m.group(1).strip() for m in re.finditer(r"([^};]*)var\(--frame-h\)", css)]
    assert bare == ["--vmin: min(1vw, calc(", ":root[data-present][data-strip] #stage { height:"]
    assert "var(--vmin)" not in css
    boot = page[page.index("<script>"):page.index("</script>")]
    present = boot[boot.index('if (q.get("present") === "1") {'):]
    taken = present[present.index('var strip = q.get("strip");'):]
    for write in ('setAttribute("data-strip"', 'setProperty("--strip"'):
        assert page.count(write) == 1 and write in taken[:taken.index("\n      }")], write
    script = page[page.index("const STRIP_BITS"):]
    assert 'if (present && document.documentElement.hasAttribute("data-strip")) {' in script
    assert page.count('"present-strip"') == 1


def test_nothing_drawn_over_the_stage_reads_the_viewports_height():
    """Under the strip the viewport is the frame and the strip below it, so a
    rule of the stage, the overlay, the card or the controls that read 100vh
    or vmin itself would lay out in the wrong height. Only the stage's own
    height outside the strip, and --frame-h's own declaration, read it."""
    css = _css(PAGE.read_text(encoding="utf-8"))
    rest = css.replace("var(--vmin, 1vmin)", "").replace("var(--frame-h, 100vh)", "")
    left = re.findall(r"[^;{}]*(?<![\w.-])\d*\.?\d+(?:vmin|vmax|[sdl]?vh)\b[^;{}]*", rest)
    assert [s.strip() for s in left] == ["height: 100vh",
                                         "--frame-h: calc(100vh - var(--strip))"], left
    js = PRESENT_JS.read_text(encoding="utf-8")
    assert "innerWidth / (innerHeight - strip)" in js
    assert js.count("innerHeight") == 1


# --------------------------------------------------------------- the page, drawn

# One browser. Each case opens its address in a fresh context at its size,
# stops the clock at 07:00 and settles as the recorder does, then takes `shots`
# screenshots of the whole viewport, a twelfth of a second of clock between
# them, each with the counter as state() said it just before the shot.
DRIVE = r"""
const { chromium } = require(process.argv[1]);
const job = JSON.parse(process.argv[2]);
(async () => {
  const browser = await chromium.launch({ channel: "chromium" });
  const out = [];
  try {
    for (const c of job.cases) {
      const ctx = await browser.newContext({ viewport: { width: c.width, height: c.height },
                                             reducedMotion: "no-preference" });
      const page = await ctx.newPage();
      const problems = [];
      page.on("pageerror", e => problems.push(e.message));
      await page.goto(c.url, { waitUntil: "load" });
      await page.waitForFunction(() => window.__present && window.__present.state);
      // The clock ran in real time until the capture stopped it, so it is
      // pinned, as the recorder pins a still's.
      await page.evaluate(() => { window.__present.setCapture(true);
                                  window.__present.seek(7 * 3600); });
      await page.evaluate(() => window.__present.settle());
      await page.evaluate(() => document.fonts && document.fonts.ready);
      if (c.card) await page.evaluate(() => window.__present.setCard(true));
      const seen = await page.evaluate(() => {
        const root = document.documentElement, stage = document.getElementById("stage");
        const strip = document.getElementById("present-strip");
        const r = strip && strip.getBoundingClientRect();
        return {
          attr: root.getAttribute("data-strip"),
          prop: root.style.getPropertyValue("--strip"),
          stage: stage.getBoundingClientRect().height,
          inner: innerHeight,
          strip: r && { top: r.top, height: r.height, left: r.left, width: r.width,
                        blocks: strip.children.length },
        };
      });
      // What each call that draws adds to the count.
      const count = () => page.evaluate(() => window.__present.state().draws);
      const steps = [];
      let before = await count();
      for (const [name, call] of [
          ["advance", () => window.__present.advance(1 / 12)],
          ["advance", () => window.__present.advance(1 / 12)],
          ["seek", () => window.__present.seek(window.__present.state().now)],
          ["settle", () => window.__present.settle()],
          ["state", () => window.__present.state()]]) {
        await page.evaluate(call);
        const after = await count();
        steps.push([name, after - before]);
        before = after;
      }
      seen.steps = steps;
      seen.shots = [];
      for (let i = 0; i < c.shots; i++) {
        if (i) await page.evaluate(() => window.__present.advance(1 / 12));
        const draws = await count();
        const png = await page.screenshot({ type: "png", animations: "allow" });
        seen.shots.push({ draws, png: png.toString("base64") });
      }
      seen.problems = problems;
      out.push(seen);
      await ctx.close();
    }
  } finally {
    await browser.close();
  }
  console.log(JSON.stringify(out));
})().catch(e => { console.error(String(e.message).split("\n")[0]); process.exit(1); });
"""


def _drive(cases: list[dict]) -> list[dict]:
    done = subprocess.run(["node", "-e", DRIVE, str(PLAYWRIGHT), json.dumps({"cases": cases})],
                          capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert all(not s["problems"] for s in seen), [s["problems"] for s in seen]
    for s in seen:
        for shot in s["shots"]:
            shot["image"] = Image.open(io.BytesIO(base64.b64decode(shot["png"]))).convert("RGB")
    return seen


def _with(url: str, **more: str) -> str:
    return url + "".join(f"&{k}={v}" for k, v in more.items())


@needs_browser
def test_the_counter_rises_by_one_a_draw_and_the_strip_says_it(tmp_path):
    """Each call that draws adds one to ``state().draws`` and reading the
    state adds none, with the strip and without it. With it, the stage is the
    viewport less the strip, the strip is the viewport's foot across its whole
    width in 32 blocks, every block's middle is pure black or pure white, and
    the strip in each shot says the count state() gave just before it."""
    page = _page(tmp_path)
    url = export.url_for(KEY, export.PRESETS["linkedin-gif"], page=page, date=DATE)
    plain, striped = _drive([
        {"url": url, "width": 320, "height": 320, "shots": 1},
        {"url": _with(url, strip=str(STRIP)), "width": 320, "height": 320 + STRIP, "shots": 4},
    ])
    for s in (plain, striped):
        assert s["steps"] == [["advance", 1], ["advance", 1], ["seek", 1], ["settle", 1],
                              ["state", 0]], s["steps"]
    assert striped["attr"] == str(STRIP) and striped["prop"] == f"{STRIP}px"
    assert striped["stage"] == striped["inner"] - STRIP == 320
    assert striped["strip"] == {"top": 320, "height": STRIP, "left": 0, "width": 320,
                                "blocks": 2 * BITS}
    counts = [shot["draws"] for shot in striped["shots"]]
    assert counts == list(range(counts[0], counts[0] + 4)), counts
    for shot in striped["shots"]:
        image = shot["image"]
        assert read_strip(image, STRIP) == shot["draws"] % (1 << BITS), shot["draws"]
        y = image.height - STRIP // 2
        for block in range(2 * BITS):
            x = int((block + 0.5) * image.width / (2 * BITS))
            assert image.getpixel((x, y)) in ((0, 0, 0), (255, 255, 255)), (block, shot["draws"])
    # The plain page's foot is the map's ground, which says no number.
    assert read_strip(plain["shots"][0]["image"], STRIP) is None


def _cases(page: str) -> list[tuple[str, str, int, int, bool]]:
    """Four frames that read the viewport: the reel at half size, with its
    zones, title, clock in the top right and a two-line caption; a landscape
    still, where vmin is the height; that still under its card; and an address
    with no frame, which fits the map to the window's own shape."""
    reel = export.plan(KEY, "instagram-reel", page=page, date=DATE, caption=SENTENCE).url
    x = export.url_for(KEY, export.PRESETS["x"], page=page, date=DATE, clock=True,
                       caption=SENTENCE)
    fill = page + "?present=1&title=1&clock=1"
    return [("reel", reel, 540, 960, False), ("x", x, 800, 450, False),
            ("x card", x, 800, 450, True), ("no frame", fill, 700, 400, False)]


@needs_browser
def test_the_frame_above_the_strip_is_the_frame_without_it(tmp_path):
    """The point of the strip: what the capture crops from above it is what it
    would have taken without it, shot for shot, in RGB within the suite's
    tolerance, where the overlay's every inset, size and zone would move by
    the strip's height if it read the viewport."""
    cases = []
    for name, url, width, height, card in _cases(_page(tmp_path)):
        cases.append({"name": name, "url": url, "width": width, "height": height,
                      "card": card, "shots": 2})
        cases.append({"name": name, "url": _with(url, strip=str(STRIP)), "width": width,
                      "height": height + STRIP, "card": card, "shots": 2})
    seen = _drive(cases)
    for i in range(0, len(seen), 2):
        plain, striped, name = seen[i], seen[i + 1], cases[i]["name"]
        assert striped["stage"] == plain["stage"] == cases[i]["height"], name
        for a, b in zip(plain["shots"], striped["shots"], strict=True):
            width, height = a["image"].size
            assert b["image"].size == (width, height + STRIP)
            cropped = b["image"].crop((0, 0, width, height))
            diff = ImageChops.difference(a["image"], cropped)
            worst = max(band[1] for band in diff.getextrema())
            assert worst <= DRIFT, f"{name}: the frame above the strip differs by {worst}/255"
            assert read_strip(b["image"], STRIP) == b["draws"] % (1 << BITS), name


@needs_browser
def test_an_address_the_page_does_not_take_sets_nothing(tmp_path):
    """Without the parameter, with one that is not a whole number from 1 to
    256, or off present mode, the root carries no attribute and no --strip,
    no strip is made and the stage is the viewport's height."""
    page = _page(tmp_path)
    url = export.url_for(KEY, export.PRESETS["linkedin-gif"], page=page, date=DATE)
    values = ["", "0", "-4", "abc", "1.5", "12px", "257", "0024", " 24"]
    cases = [{"url": url, "width": 320, "height": 320, "shots": 0}]
    cases += [{"url": _with(url, strip=v), "width": 320, "height": 320, "shots": 0}
              for v in values]
    cases.append({"url": page + "?strip=24", "width": 320, "height": 320, "shots": 0})
    seen = _drive(cases)
    for case, s in zip(cases, seen, strict=True):
        assert (s["attr"], s["prop"], s["strip"]) == (None, "", None), case["url"]
        assert s["stage"] == s["inner"] or "present=1" not in case["url"], case["url"]
    # The edge of what it takes.
    edge = _drive([{"url": _with(url, strip=v), "width": 320, "height": 320 + int(v),
                    "shots": 1} for v in ("1", "256")])
    for v, s in zip(("1", "256"), edge, strict=True):
        assert s["attr"] == v and s["stage"] == 320, v
