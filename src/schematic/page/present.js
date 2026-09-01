// Presentation mode: policy, on top of the mechanism window.__present exposes.
//
// Everything here is driven by the URL, so a city can be opened clean in a
// browser to present from or to screen-record by hand, and the exporter is only
// another caller rather than a second renderer:
//
//   maps/cdmx-metro.html?present=1&view=map&labels=0&title=1&clock=1&frame=9:16
//
// Nothing in here reaches into the page's internals; it talks only through
// window.__present.
(function () {
  var P = window.__present;
  if (!P) return;
  var q = new URLSearchParams(location.search);
  if (q.get("present") !== "1") return;

  var num = function (key, fallback) {
    var v = parseFloat(q.get(key));
    return isFinite(v) ? v : fallback;
  };
  var on = function (key, fallback) {
    var v = q.get(key);
    return v === null ? fallback : v === "1";
  };

  // ------------------------------------------------------------------- frame
  // "9:16" -> 0.5625. The box is padded to this aspect inside the SVG rather
  // than the SVG being letterboxed inside the page: the page background and the
  // map background are not the same colour in the dark theme, so a letterbox
  // would show a seam -- and only a padded box gives a still and a video of the
  // same preset an identical frame.
  // Slightly more of the added height goes above the network, because that is
  // where the name sits and a title wants air above it. The margin is wider
  // when there is an overlay, so the text has ground of its own.
  var titled = q.get("title") === "1" || q.get("clock") === "1";
  var top = num("frametop", 0.46);
  var margin = num("margin", titled ? 0.085 : 0.025);

  var frame = q.get("frame");
  var fixed = null;
  if (frame) {
    var parts = frame.split(":").map(Number);
    if (parts.length === 2 && parts[0] > 0 && parts[1] > 0) fixed = parts[0] / parts[1];
  }

  // With no frame given -- someone opening this in a browser to present from,
  // rather than an export asking for a specific aspect -- fill the window, and
  // keep filling it when the window changes.
  function fit() {
    P.setFrame(fixed || (innerWidth / innerHeight), top, margin);
  }
  fit();
  if (!fixed) addEventListener("resize", fit);

  // ------------------------------------------------------------------- state
  var view = q.get("view") || "map";
  // "geographic" is the map view with the geographic axis raised, not a fourth
  // renderer -- so it is a view to a caller and two calls in here. A page built
  // without geographic geometry ignores it and shows the schematic map; the
  // exporter refuses the export before it gets this far, which is where that
  // belongs.
  P.setGeo(view === "geographic", 0);
  if (view === "geographic") view = "map";
  if (view === "time") view = "string";        // the page's internal name
  P.setView(view);
  P.setLabels(on("labels", true));
  if (q.get("lines")) P.setRoutes(q.get("lines").split(","));
  P.setSpeed(num("speed", 60));

  var at = q.get("at");
  if (at) {
    var hm = at.split(":").map(Number);
    if (hm.length >= 2) P.seek(hm[0] * 3600 + hm[1] * 60 + (hm[2] || 0) * 1);
  }
  if (!on("play", true)) P.setPlaying(false);
  P.settle();

  // ---------------------------------------------------------------- sequence
  // `sequence=transform` runs the argument the essay is making, on a loop: the
  // network as it sits on the ground, straightening into the schematic map,
  // then unfolding into a row per line. Each step throws away more of the
  // geography and is easier to read for it, which is the point being made.
  //
  // A figure running this gets no controls of its own -- the sequence *is* the
  // content, and a switcher beside it invites the reader to interrupt the
  // sentence halfway through.
  if (q.get("sequence") === "transform" && P.hasGeo && P.hasGeo()) {
    if (matchMedia("(prefers-reduced-motion: reduce)").matches) {
      // Show the finished map and leave it alone. The trains still run: that is
      // the timetable, not decoration, and it is not what "reduce motion" means.
      P.setGeo(false, 0);
    } else {
      var MORPH = 1.8, HOLD = 2.6;
      // There and back, so the loop reads as a rewind rather than a jump cut.
      var steps = [
        function () { P.setGeo(false, MORPH); },   // ground -> schematic
        function () { P.setView("linear", MORPH); },  // schematic -> rows
        function () { P.setView("map", MORPH); },     // rows -> schematic
        function () { P.setGeo(true, MORPH); },    // schematic -> ground
      ];
      var at = 0;
      P.setGeo(true, 0);
      setTimeout(function step() {
        steps[at]();
        at = (at + 1) % steps.length;
        setTimeout(step, (MORPH + HOLD) * 1000);
      }, HOLD * 1000);
    }
  }

  // ----------------------------------------------------------------- overlay
  var box = document.getElementById("present-overlay");
  var nameEl = box.querySelector(".name");
  var timeEl = box.querySelector(".time");

  var showName = on("title", false);
  var showClock = on("clock", false);
  nameEl.hidden = !showName;
  timeEl.hidden = !showClock;

  if (showName) {
    // Two lines, because a city and its network are two facts. The page's own
    // <h1> carries whatever the pipeline was given; city/network split it.
    var city = q.get("city") || "";
    var network = q.get("network") || "";
    if (!city && !network) {
      city = (document.querySelector("h1") || {}).textContent || "";
    }
    nameEl.querySelector("b").textContent = city;
    nameEl.querySelector("span").textContent = network;
  }

  if (showClock) {
    P.onDraw(function () { timeEl.textContent = P.state().clock; });
  }

  // --------------------------------------------------------------- safe area
  // Where Instagram's own controls sit over a 9:16 frame. A preview aid only --
  // the exporter writes this into a separate file and never into a deliverable.
  if (q.get("safe") === "1") {
    var zones = [
      { top: "0", left: "0", width: "100%", height: "12%" },   // status bar, top actions
      { bottom: "0", left: "0", width: "100%", height: "22%" },// caption, handle, audio
      { top: "40%", right: "0", width: "18%", height: "38%" }, // the button rail
    ];
    var layer = document.createElement("div");
    layer.id = "present-safe";
    zones.forEach(function (z) {
      var d = document.createElement("div");
      // Only the sides the zone actually names, or an unset edge resolves to 0
      // and stretches the box across the frame.
      ["top", "bottom", "left", "right", "width", "height"].forEach(function (k) {
        if (z[k] !== undefined) d.style[k] = z[k];
      });
      layer.appendChild(d);
    });
    document.body.appendChild(layer);
  }
})();
