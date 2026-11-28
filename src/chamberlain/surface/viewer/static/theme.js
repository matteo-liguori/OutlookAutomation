/* The theme switch: Match system (default), Light or Dark. Loaded in <head> without defer so the
   saved choice is applied before first paint. It sets data-theme on <html> (none = match system),
   keeps the choice in localStorage and follows the system while "system" is chosen. */
(function () {
  "use strict";
  var KEY = "chamberlain.viewer.theme";
  var CHOICES = [["system", "Match system"], ["light", "Light"], ["dark", "Dark"]];
  var BAR = { light: "#ffffff", dark: "#222b27" };
  var root = document.documentElement;
  var query = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

  function saved() {
    try {
      var value = window.localStorage.getItem(KEY);
      return value === "light" || value === "dark" ? value : "system";
    } catch (ignored) { return "system"; }
  }

  function apply(choice) {
    if (choice === "system") root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", choice);
    var dark = choice === "dark" || (choice === "system" && !!query && query.matches);
    var meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark ? BAR.dark : BAR.light);
  }

  var choice = saved();
  apply(choice);
  if (query && query.addEventListener) {
    query.addEventListener("change", function () { if (choice === "system") apply(choice); });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("theme-select");
    if (!select) return;
    CHOICES.forEach(function (entry) {
      var option = document.createElement("option");
      option.value = entry[0];
      option.textContent = entry[1];
      select.appendChild(option);
    });
    select.value = choice;
    select.addEventListener("change", function () {
      choice = select.value;
      try {
        if (choice === "system") window.localStorage.removeItem(KEY);
        else window.localStorage.setItem(KEY, choice);
      } catch (ignored) { /* the choice still applies for this window */ }
      apply(choice);
    });
  });
})();
