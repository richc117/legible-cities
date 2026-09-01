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

    var done = false;
    function ready() {
      if (done) return;
      var P = seam(frame);
      if (!P) return;
      done = true;

      btns.forEach(function (btn) {
        btn.disabled = false;
        btn.addEventListener("click", function () {
          // present.js renames "time" to the page's internal "string" inside
          // its own IIFE, so a direct caller of __present has to do it here.
          var v = btn.dataset.view;
          try {
            P.setView(v === "time" ? "string" : v);
          } catch (e) {
            return;
          }
          btns.forEach(function (other) {
            other.setAttribute("aria-pressed", String(other === btn));
          });
        });
      });
    }

    frame.addEventListener("load", ready);
    // An eager frame can finish loading before this deferred script runs, in
    // which case its load event is already gone.
    try {
      if (frame.contentDocument && frame.contentDocument.readyState === "complete") ready();
    } catch (e) {}
  }

  document.querySelectorAll(".figure--embed").forEach(wire);
})();
