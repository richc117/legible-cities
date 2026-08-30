#!/usr/bin/env node
// Capture the animation page's presentation mode, one frame at a time.
//
// Not Playwright's recordVideo: that records in real time, so no two runs agree,
// and it encodes to VP8 which then has to be transcoded. Here the page's clock
// is stepped by hand -- exactly 1/fps per frame -- so the same command twice
// produces the same frames, and every frame is a full-quality PNG.
//
// Driven by schematic/export.py, which passes one JSON job as argv[2], the same
// shape bin/shoot uses. Not meant to be run by hand.
const fs = require("fs");
const path = require("path");

let chromium;
try {
  ({ chromium } = require(path.join(__dirname, "..", "site", "node_modules", "playwright")));
} catch (e) {
  console.error("playwright missing. cd site && npm install --no-save playwright && npx playwright install chromium");
  process.exit(1);
}

const job = JSON.parse(process.argv[2] || "{}");
const hms = t => { const p = String(t).split(":").map(Number);
                   return (p[0] || 0) * 3600 + (p[1] || 0) * 60 + (p[2] || 0); };

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({
    viewport: { width: job.width, height: job.height },
    deviceScaleFactor: job.scale || 1,
    // Without this the page may inherit "reduce" and snap every transition,
    // turning the morph storyboard into a series of hard cuts.
    reducedMotion: "no-preference",
  });
  const page = await ctx.newPage();
  const problems = [];
  page.on("pageerror", e => problems.push("pageerror: " + e.message));
  page.on("console", m => { if (m.type() === "error") problems.push("console: " + m.text()); });

  await page.goto(job.url, { waitUntil: "load" });
  await page.waitForFunction(() => window.__present && window.__present.state, null,
                             { timeout: 60000 });
  // The payload is megabytes on the larger networks; let the first geometry
  // pass and the fonts settle before anything is measured or captured.
  await page.evaluate(() => document.fonts && document.fonts.ready);
  await page.waitForTimeout(job.settle || 1200);

  const bounds = await page.evaluate(() => window.__present.bounds());
  const first = await page.evaluate(() => window.__present.state());

  // An `at` outside the service day renders a correct, empty map -- and would
  // otherwise capture several hundred frames of nothing before anyone noticed.
  if (!first.shown) {
    const f = s => String(Math.floor(s / 3600)).padStart(2, "0") + ":" +
                   String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    problems.push(`no trains at ${first.clock}; this feed runs ${f(bounds.t0)}-${f(bounds.t1)}`);
  }
  if (problems.length) {
    console.error("FAIL  " + problems.join("\n      "));
    await browser.close();
    process.exit(1);
  }

  const stage = await page.$("#stage");
  const shot = out => stage.screenshot({ path: out, type: job.format === "jpg" ? "jpeg" : "png",
                                         quality: job.format === "jpg" ? 92 : undefined });

  if (job.mode === "still") {
    await page.evaluate(() => window.__present.settle());
    await shot(job.out);
    console.log(`ok    still ${path.basename(job.out)}  ${first.shown} trains  ${first.clock}`);
  } else {
    fs.mkdirSync(job.frames, { recursive: true });
    // Stop the real-time loop and settle every tween *before* the first beat,
    // so nothing from the page's own startup leaks into frame 0.
    await page.evaluate(() => { window.__present.setCapture(true);
                                window.__present.settle(); });
    const fps = job.fps;
    let n = 0;
    const t0 = Date.now();

    for (const beat of job.beats) {
      const frames = Math.round(beat.secs * fps);
      // Everything the beat names is applied at its start; anything it leaves
      // out carries over from the beat before.
      await page.evaluate(b => {
        const P = window.__present;
        if (b.at != null) P.seek(b.at);
        if (b.labels != null) P.setLabels(b.labels);
        if (b.speed != null) P.setSpeed(b.speed);
        if (b.view) P.setView(b.view === "time" ? "string" : b.view, b.tween);
        P.setPlaying(!b.sweep && b.speed !== 0);
      }, beat);

      // A sweep given `hours` starts wherever the clock already is, so the
      // storyboard never jumps backwards between beats.
      let lo = beat.lo, hi = beat.hi;
      if (beat.sweep && beat.hours) {
        lo = (await page.evaluate(() => window.__present.state())).now;
        hi = Math.min(lo + beat.hours * 3600, bounds.t1);
      }

      for (let i = 0; i < frames; i++) {
        if (beat.sweep) {
          // The sweep owns the clock outright, so a whole day can pass in a few
          // seconds without the playback rate having to be absurd.
          const p = frames > 1 ? i / (frames - 1) : 1;
          await page.evaluate(s => window.__present.seek(s), lo + (hi - lo) * p);
        } else {
          await page.evaluate(dt => window.__present.advance(dt), 1 / fps);
        }
        await shot(path.join(job.frames, String(n).padStart(6, "0") + ".png"));
        n++;
      }
    }
    const secs = (Date.now() - t0) / 1000;
    console.log(`ok    ${n} frames in ${secs.toFixed(0)}s ` +
                `(${(secs / n * 1000).toFixed(0)}ms/frame)`);
  }

  await browser.close();
  process.exit(0);
})();
