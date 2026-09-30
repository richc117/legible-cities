// The route lines' finish (routes.css says what it looks like).
//
// The engine draws each line as many short segments, one per pair of adjacent
// stations. A glow, a rim or a travelling light drawn per segment would break
// at every station, so this joins each line's segments into whole routes by
// the stations they share, and lays the extra strokes along those. The routes
// are rebuilt from the segments every frame, so they follow every morph and
// every hidden line the page makes without the page knowing they are there.
//
// Motion never reaches an export: a page loaded with a `frame` (every export
// URL carries one) starts still, and setCapture(true) stills it at once, so a
// captured frame depends only on the clock the exporter sets.
(function () {
  var NS = "http://www.w3.org/2000/svg";
  var root = document.documentElement;
  var mk = function (n, a, p) {
    var e = document.createElementNS(NS, n);
    for (var k in a) e.setAttribute(k, a[k]);
    if (p) p.appendChild(e);
    return e;
  };

  // The atlas's thumbnails are twenty-odd maps on one screen: they keep the
  // plain lines, which is also what keeps that page light.
  var all = [].filter.call(document.querySelectorAll('svg [id="lines"]'), function (g) {
    return !g.closest(".atlas");
  });
  if (!all.length) return;

  var still = new URLSearchParams(location.search).has("frame") ||
              matchMedia("(prefers-reduced-motion: reduce)").matches;
  var finishers = [];
  function goStill() {
    if (root.classList.contains("rs-still")) return;
    root.classList.add("rs-still");
    finishers.forEach(function (f) { f(); });
  }
  if (still) root.classList.add("rs-still");
  if (window.__present && window.__present.setCapture) {
    var setCapture = window.__present.setCapture;
    window.__present.setCapture = function (on) {
      if (on) goStill();
      return setCapture.apply(this, arguments);
    };
  }

  // One set of filters serves every map on the page: url(#id) resolves
  // document-wide.
  var first = all[0].ownerSVGElement;
  var defs = first.querySelector("defs") || first.insertBefore(mk("defs", {}), first.firstChild);
  [["rs-bloom", "9"], ["rs-shadow", "2.5"], ["rs-glow-sm", "1.6"]].forEach(function (f) {
    mk("feGaussianBlur", { stdDeviation: f[1] },
       mk("filter", { id: f[0], x: "-30%", y: "-30%", width: "160%", height: "160%" }, defs));
  });

  // A line's colour mixed toward black (0) or white (255).
  function mix(hex, to, t) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return hex;
    var n = parseInt(m[1], 16), c = [n >> 16, (n >> 8) & 255, n & 255];
    return "rgb(" + c.map(function (v) { return Math.round(v + (to - v) * t); }).join(",") + ")";
  }

  all.forEach(finish);

  function finish(lines) {
    var svg = lines.ownerSVGElement, parent = lines.parentNode;
    var inSvg = function (id) { return svg.querySelector('[id="' + id + '"]'); };
    var under = mk("g", { class: "rs-under", "pointer-events": "none" });
    parent.insertBefore(under, lines);
    var over = mk("g", { class: "rs-over", "pointer-events": "none" });
    parent.insertBefore(over, lines.nextSibling);
    var cores = mk("g", {}, over), intro = mk("g", {}, over), glints = mk("g", { class: "rs-glints" }, over);

    // Walk each line's segments from station to station until a junction or an end.
    var routes = [];
    lines.querySelectorAll("g.line").forEach(function (line, li) {
      var segs = [].slice.call(line.querySelectorAll("path[data-src]")), at = {}, used = new Set();
      segs.forEach(function (p) {
        (at[p.dataset.src] = at[p.dataset.src] || []).push(p);
        (at[p.dataset.dst] = at[p.dataset.dst] || []).push(p);
      });
      var walk = function (node, seg) {
        var chain = [], p = seg, n = node;
        while (p && !used.has(p)) {
          used.add(p);
          var fwd = p.dataset.src === n;
          chain.push({ p: p, rev: !fwd });
          n = fwd ? p.dataset.dst : p.dataset.src;
          var next = at[n].filter(function (q) { return !used.has(q); });
          p = at[n].length === 2 && next.length === 1 ? next[0] : null;
        }
        return chain;
      };
      Object.keys(at).filter(function (n) { return at[n].length !== 2; }).forEach(function (n) {
        at[n].forEach(function (p) { if (!used.has(p)) routes.push({ line: line, li: li, chain: walk(n, p) }); });
      });
      // What is left is a loop, with no end to start from.
      segs.forEach(function (p) { if (!used.has(p)) routes.push({ line: line, li: li, chain: walk(p.dataset.src, p) }); });
    });

    routes.forEach(function (r, i) {
      var c = r.line.getAttribute("stroke");
      r.ambient = mk("path", { class: "ambient", stroke: c });
      r.gap = mk("path", { class: "gap" });
      r.rim = mk("path", { class: "rim", stroke: mix(c, 0, 0.38) });
      r.core = mk("path", { class: "core", stroke: mix(c, 255, 0.55) }, cores);
      r.glint = mk("path", { class: "glint" }, glints);
      // Staggered, so the lights never run in step.
      var period = 6 + (i * 1.9) % 5;
      r.glint.style.setProperty("--rs-period", period + "s");
      r.glint.style.setProperty("--rs-delay", "-" + ((i * 3.1) % period) + "s");
    });
    // Every glow, then every gap, then every rim: a line crossing another
    // reads as passing over it rather than merging into it.
    ["ambient", "gap", "rim"].forEach(function (k) {
      routes.forEach(function (r) { under.appendChild(r[k]); });
    });

    var cache = new Map();
    function points(p) {
      var d = p.getAttribute("d"), c = cache.get(p);
      if (c && c.d === d) return c.pts;
      var n = (d.match(/-?\d*\.?\d+(?:e-?\d+)?/gi) || []).map(Number), out = [];
      for (var i = 0; i + 1 < n.length; i += 2) out.push(n[i].toFixed(2) + " " + n[i + 1].toFixed(2));
      cache.set(p, { d: d, pts: out });
      return out;
    }
    function outline(chain) {
      var all = [];
      chain.forEach(function (s, k) {
        var q = points(s.p);
        if (s.rev) q = q.slice().reverse();
        all.push.apply(all, k ? q.slice(1) : q);
      });
      return all.length ? "M" + all.join(" L") : "";
    }

    var parts = ["ambient", "gap", "rim", "core", "glint"];
    function sync() {
      var op = lines.getAttribute("opacity");
      [under, over].forEach(function (g) {
        if (op === null) g.removeAttribute("opacity"); else g.setAttribute("opacity", op);
      });
      routes.forEach(function (r) {
        var d = outline(r.chain), hide = r.line.style.display;
        if (r.d !== d) {
          r.d = d;
          parts.forEach(function (k) { r[k].setAttribute("d", d); });
          if (r.intro) r.intro.setAttribute("d", d);
        }
        parts.forEach(function (k) { r[k].style.display = hide; });
      });
      requestAnimationFrame(sync);
    }
    sync();

    if (root.classList.contains("rs-still")) return;

    // Draw each line in, a beat after the one before; then the stations and
    // names settle on, and the lights begin.
    var order = [];
    routes.forEach(function (r) { if (order.indexOf(r.li) < 0) order.push(r.li); });
    var STEP = 0.2, DUR = 1.2, end = 0.15 + order.length * STEP + DUR;
    lines.classList.add("rs-hide");
    under.style.opacity = "0";
    cores.style.opacity = "0";
    under.style.transition = cores.style.transition = "opacity .6s ease " + (end - 0.6) + "s";
    routes.forEach(function (r) {
      var e = mk("path", { d: r.d, stroke: r.line.getAttribute("stroke"), "stroke-width": "7" }, intro);
      var len = e.getTotalLength() || 1;
      e.style.strokeDasharray = len;
      e.style.strokeDashoffset = len;
      e.style.transition = "stroke-dashoffset " + DUR + "s cubic-bezier(0.2, 0, 0, 1) " +
        (0.15 + order.indexOf(r.li) * STEP) + "s";
      r.intro = e;
    });
    var fades = ["stations", "labels", "trains"].map(inSvg).filter(Boolean);
    fades.forEach(function (g) {
      g.style.setProperty("--rs-after", (end - 0.4) + "s");
      g.classList.add("rs-fade");
    });
    requestAnimationFrame(function () {
      requestAnimationFrame(function () {
        routes.forEach(function (r) { if (r.intro) r.intro.style.strokeDashoffset = 0; });
        under.style.opacity = "";
        cores.style.opacity = "";
      });
    });
    var done = false;
    function settle() {
      if (done) return;
      done = true;
      lines.classList.remove("rs-hide");
      intro.remove();
      routes.forEach(function (r) { r.intro = null; });
      under.style.transition = cores.style.transition = "";
      under.style.opacity = cores.style.opacity = "";
      glints.classList.add("on");
    }
    setTimeout(settle, end * 1000 + 80);
    // A capture that starts mid-intro gets the finished map at once.
    finishers.push(function () {
      settle();
      fades.forEach(function (g) { g.classList.remove("rs-fade"); });
    });
  }
})();
