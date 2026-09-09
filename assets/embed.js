// View controls for the essay's embedded maps.
//
// Each embed is the generated animation page in presentation mode, in an
// iframe. That page exposes exactly one seam -- window.__present -- and this
// talks only through it, the same way bin/export does. Everything else in the
// page is closed over inside its IIFE and deliberately out of reach.
//
// The theme is not handled here: the animation page reads the shared rc-theme
// key from localStorage before paint, so a frame always loads correct. Only a
// toggle while a frame is already open needs propagating, and theme.js does it.
(function () {
  // The order the switcher walks, and the order the argument runs in: where
  // the network lies on the ground, straightened onto the grid, unfolded into a
  // row per line, then re-read as time.
  var ORDER = ["geographic", "map", "linear", "time"];

  function pressedView(btns) {
    for (var i = 0; i < btns.length; i++) {
      if (btns[i].getAttribute("aria-pressed") === "true") return btns[i].dataset.view;
    }
    return null;
  }

  // The landing page figure's own timing. Third copy of two numbers that also
  // live in present.js (`var MORPH = 1.8, HOLD = 2.6`) and export.py
  // (PAGE_MORPH / PAGE_HOLD); test_export.py reads all three so they cannot
  // drift. A beat is one morph plus one hold.
  var MORPH = 1.8, HOLD = 2.6;
  // How long a reader's click keeps the figure to itself before it resumes.
  var RESUME = 15;

  // Same-origin, so this is a plain property read -- but a frame that has not
  // booted yet, or one served from somewhere else, must degrade to a still
  // animation rather than a console error.
  function seam(frame) {
    try {
      return (frame.contentWindow && frame.contentWindow.__present) || null;
    } catch (e) {
      return null;
    }
  }

  function wire(fig) {
    var frame = fig.querySelector(".embed-frame");
    var btns = [].slice.call(fig.querySelectorAll(".segmented button[data-view]"));
    if (!frame || !btns.length) return;

    // Cycling is declared per figure in the template, not offered as a control:
    // it is the figure making its own argument, and only the essay's opening
    // one does it.
    var cycling = fig.dataset.cycle === "1" &&
      !matchMedia("(prefers-reduced-motion: reduce)").matches;
    // A copy per figure: a network without the geographic geometry drops that
    // step, and must not drop it from the other figures on the page.
    var order = ORDER.slice();
    var P = null, timer = null, idle = null, visible = true, at = 0;

    function press(view) {
      btns.forEach(function (b) {
        b.setAttribute("aria-pressed", String(b.dataset.view === view));
      });
    }

    function show(view, dur) {
      try {
        // Both axes at once, by the public name of the view. present.js does
        // the same translation for its own URL parameter, but inside its IIFE.
        P.showView(view, dur);
      } catch (e) {
        return false;
      }
      press(view);
      return true;
    }

    function stop() {
      if (timer) { clearTimeout(timer); timer = null; }
    }

    // Runs only while the figure is on screen and the tab is in front: a figure
    // nobody is looking at should not be stepping itself, and a view that
    // changed unwatched is a view the reader never saw change.
    function play() {
      if (!cycling || timer || !visible) return;
      timer = setTimeout(function step() {
        at = (at + 1) % order.length;
        if (!show(order[at], MORPH)) { timer = null; return; }
        timer = setTimeout(step, (MORPH + HOLD) * 1000);
      }, HOLD * 1000);
    }

    // A click hands the figure to the reader. It comes back after RESUME
    // seconds of being left alone, from wherever they left it.
    function yieldToReader() {
      if (!cycling) return;
      stop();
      if (idle) clearTimeout(idle);
      idle = setTimeout(function () { idle = null; play(); }, RESUME * 1000);
    }

    var done = false;
    function ready() {
      if (done) return;
      P = seam(frame);
      if (!P) return;
      done = true;

      btns.forEach(function (btn) {
        btn.disabled = false;
        btn.addEventListener("click", function () {
          yieldToReader();
          if (show(btn.dataset.view)) at = order.indexOf(btn.dataset.view);
        });
      });

      // A network without the geographic geometry has nothing to show for that
      // button; the page drops its own copy for the same reason.
      try {
        if (P.hasGeo && !P.hasGeo()) {
          btns = btns.filter(function (b) {
            if (b.dataset.view !== "geographic") return true;
            b.remove();
            return false;
          });
          order = order.filter(function (v) { return v !== "geographic"; });
          // The figure asked to open geographic and cannot: show what it does
          // have, so the switcher and the map agree from the first frame.
          if (!pressedView(btns)) show(order[0]);
        }
      } catch (e) {}

      // Start the cycle from whatever the figure opened on.
      at = Math.max(0, order.indexOf(pressedView(btns)));

      if (cycling && "IntersectionObserver" in window) {
        new IntersectionObserver(function (entries) {
          visible = entries[0].isIntersecting;
          if (visible) play(); else stop();
        }, { threshold: 0.25 }).observe(fig);
      } else {
        play();
      }
    }

    document.addEventListener("visibilitychange", function () {
      if (document.hidden) stop();
      else if (!idle) play();
    });

    frame.addEventListener("load", ready);
    // An eager frame can finish loading before this deferred script runs, in
    // which case its load event is already gone.
    try {
      if (frame.contentDocument && frame.contentDocument.readyState === "complete") ready();
    } catch (e) {}
  }

  document.querySelectorAll(".figure--embed").forEach(wire);
})();
