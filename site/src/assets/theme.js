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
