// Presentation mode: policy, on top of the mechanism window.__present exposes.
//
// Everything here is driven by the URL, so a city can be opened clean in a
// browser to present from or to screen-record by hand, and the exporter is only
// another caller rather than a second renderer:
//
//   maps/cdmx-metro.html?present=1&view=map&labels=0&title=1&clock=1&frame=9:16
//
// `controls=1` adds the view switcher, so a presentation can be steered. It is
// off by default and the exporter never sets it: anything visible in present
// mode is captured into the frame.
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
  var titled = q.get("title") === "1" || q.get("clock") === "1" || !!q.get("caption");
  var top = num("frametop", 0.46);
  var margin = num("margin", titled ? 0.085 : 0.025);

  // Where the platform draws its own interface over the frame, as fractions of
  // it (export.SAFE_ZONES, which the plan writes onto the address; none of the
  // numbers live here). The frame is laid out in the part under the top zone:
  // the stylesheet reserves the zone on the stage and moves the name and the
  // clock below it and off the sides, and the box is padded to the aspect of
  // what is left. None of it happens on an address that carries no zone.
  var fraction = function (key) {
    var v = num(key, NaN);
    return v >= 0 && v < 1 ? v : null;
  };
  var zones = { top: fraction("ztop"), bottom: fraction("zbottom"), side: fraction("zside"),
                rail: fraction("zrail"), railTop: fraction("zrailtop") };
  var below = 1;
  if (zones.top !== null || zones.bottom !== null || zones.side !== null || zones.rail !== null) {
    var root = document.documentElement;
    root.setAttribute("data-zones", "");
    if (zones.top !== null) {
      root.style.setProperty("--zone-top", String(zones.top));
      below = 1 - zones.top;
    }
    if (zones.side !== null) root.style.setProperty("--zone-side", String(zones.side));
  }

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
    P.setFrame((fixed || (innerWidth / innerHeight)) / below, top, margin);
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
  // "schematic" is what the switcher calls it now. The key stayed `map`,
  // because it is written into every atlas link and every storyboard, so both
  // spellings have to arrive at the same view.
  if (view === "schematic") view = "map";
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
  // The view switcher, for a presentation opened in a browser rather than one
  // being captured. Off by default, and that default is what keeps it out of
  // every export: url_for() never sets it, so a recorded frame is exactly what
  // it was before this existed. A document-level attribute rather than a style,
  // the way `present` itself is set -- the stylesheet acts on it, and nothing
  // in here reaches into the page.
  if (on("controls", false)) {
    document.documentElement.setAttribute("data-controls", "");
  }

  var box = document.getElementById("present-overlay");
  var nameEl = box.querySelector(".name");
  var timeEl = box.querySelector(".time");
  var captionEl = nameEl.querySelector(".caption");

  var showName = on("title", false);
  var showClock = on("clock", false);
  // A person's own words for the frame, under the name: set as text, so a
  // caption holding markup is drawn as the characters it is. With the title
  // off it is the name block's only line.
  var caption = q.get("caption") || "";
  nameEl.hidden = !(showName || caption);
  timeEl.hidden = !showClock;
  if (caption) {
    captionEl.textContent = caption;
    captionEl.hidden = false;
    box.setAttribute("data-caption", "");
  }
  // The clock's corner. Bottom right is the stylesheet's own rule and needs no
  // attribute, so an address that names no corner lays out as it always did.
  var corner = q.get("corner");
  if (["top-left", "top-right", "bottom-left"].indexOf(corner) >= 0) {
    box.setAttribute("data-corner", corner);
  }

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
    // Which service day this is. An exported clock reading 07:14 says nothing
    // about *when*, and these feeds are snapshots -- the atlas names the date
    // beside every network, so an image that travels on its own should too.
    var when = q.get("date") || "";
    var whenEl = nameEl.querySelector(".when");
    whenEl.textContent = when;
    whenEl.hidden = !when;
  }

  if (showClock) {
    P.onDraw(function () { timeEl.textContent = P.state().clock; });
  }

  // --------------------------------------------------------------- safe area
  // Where the platform's own controls sit over the frame: a box for each zone
  // the address carries, and nothing when it carries none. A preview aid only
  // -- the exporter writes this into a separate file and never into a
  // deliverable, and the numbers are the plan's, never this file's.
  if (on("safe", false)) {
    var pct = function (v) { return v * 100 + "%"; };
    var boxes = [];
    if (zones.top !== null) {            // status bar, top actions
      boxes.push({ top: "0", left: "0", width: "100%", height: pct(zones.top) });
    }
    if (zones.bottom !== null) {         // caption, handle, audio
      boxes.push({ bottom: "0", left: "0", width: "100%", height: pct(zones.bottom) });
    }
    if (zones.side !== null) {           // the margins either side
      boxes.push({ top: "0", left: "0", width: pct(zones.side), height: "100%" });
      boxes.push({ top: "0", right: "0", width: pct(zones.side), height: "100%" });
    }
    if (zones.rail !== null) {           // the button rail, down to the bottom edge
      boxes.push({ top: zones.railTop === null ? "0" : pct(zones.railTop), bottom: "0",
                   right: "0", width: pct(zones.rail) });
    }
    var layer = document.createElement("div");
    layer.id = "present-safe";
    boxes.forEach(function (z) {
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
