"use strict";
// Light / dark theme. Loaded in <head> (not deferred) so the page never flashes the wrong theme.
// Choice is remembered in this browser only; with no choice the phone/computer setting is followed.
(function () {
  var KEY = "mc_theme";
  function saved() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function systemDark() { return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches; }
  function apply(t) { document.documentElement.setAttribute("data-theme", t); }
  apply(saved() || (systemDark() ? "dark" : "light"));
  window.toggleTheme = function () {
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    apply(next);
    try { localStorage.setItem(KEY, next); } catch (e) {}
    return next;
  };
  document.addEventListener("DOMContentLoaded", function () {
    var b = document.getElementById("themeToggle");
    if (!b) return;
    function label() {
      var dark = document.documentElement.getAttribute("data-theme") === "dark";
      b.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
      b.title = b.getAttribute("aria-label");
    }
    label();
    b.addEventListener("click", function () { window.toggleTheme(); label(); });
  });
})();
