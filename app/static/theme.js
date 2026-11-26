// Theme: dark by default, light for reading. Loaded in <head> without defer so
// the attribute is set before first paint (no flash of the wrong theme).
(function () {
  var KEY = "repo-ripper-theme";
  var root = document.documentElement;

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function apply(theme) { root.setAttribute("data-theme", theme); }

  var initial = stored();
  if (initial !== "light" && initial !== "dark") {
    initial = window.matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
  }
  apply(initial);

  document.addEventListener("click", function (ev) {
    if (!ev.target.closest("[data-theme-toggle]")) return;
    var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
    apply(next);
    try { localStorage.setItem(KEY, next); } catch (e) { /* private mode: page-only */ }
  });
})();
