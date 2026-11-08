/* Light or dark: follows the system unless the visitor picked one (kept in localStorage). */
(function () {
  "use strict";
  var KEY = "keyhold-theme";
  var root = document.documentElement;
  try {
    var saved = window.localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) { /* storage blocked: follow the system */ }

  function effective() {
    var picked = root.getAttribute("data-theme");
    if (picked) return picked;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  document.addEventListener("DOMContentLoaded", function () {
    var button = document.querySelector("[data-theme-toggle]");
    if (!button) return;
    function sync() { button.setAttribute("aria-pressed", effective() === "dark" ? "true" : "false"); }
    sync();
    if (window.matchMedia) {
      var query = window.matchMedia("(prefers-color-scheme: dark)");
      if (query.addEventListener) query.addEventListener("change", sync);
    }
    button.addEventListener("click", function () {
      var next = effective() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { window.localStorage.setItem(KEY, next); } catch (e) { /* not saved, still applied */ }
      sync();
    });
  });
})();
