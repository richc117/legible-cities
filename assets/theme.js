(function () {
  var root = document.documentElement;
  var key = "rc-theme";
  var defaultTheme = "warm-dark";
  var stored = localStorage.getItem(key) || defaultTheme;

  function applyTheme(value) {
    if (value === "sepia") {
      root.setAttribute("data-theme", "sepia");
    } else {
      root.removeAttribute("data-theme");
    }

    // The essay's embedded maps are same-origin iframes. They read this same
    // key before paint, so one that loads later is already correct -- it is
    // only a toggle while a frame is open that has to be pushed across, and
    // leaving it out gives four dark maps on a sepia page.
    document.querySelectorAll("iframe.embed-frame").forEach(function (frame) {
      try {
        var inner = frame.contentDocument.documentElement;
        if (value === "sepia") {
          inner.setAttribute("data-theme", "sepia");
        } else {
          inner.removeAttribute("data-theme");
        }
      } catch (e) {}
    });
  }

  applyTheme(stored);

  document.addEventListener("DOMContentLoaded", function () {
    var toggle = document.getElementById("theme-toggle");
    if (!toggle) return;

    function currentTheme() {
      return root.getAttribute("data-theme") === "sepia" ? "sepia" : "warm-dark";
    }

    function syncToggleState() {
      toggle.checked = currentTheme() === "warm-dark";
    }

    toggle.addEventListener("change", function () {
      var next = toggle.checked ? "warm-dark" : "sepia";
      localStorage.setItem(key, next);
      applyTheme(next);
    });

    syncToggleState();
  });
})();
