// Setup wizard, step 3: client-side filters and selection for the repository
// list. The list arrives by an HTMX swap, so every handler is delegated.
(function () {
  function box() { return document.getElementById("repo-picker"); }
  function rows() {
    return Array.prototype.slice.call(document.querySelectorAll("#repo-picker tr[data-name]"));
  }
  function filter(name) {
    return document.querySelector('#repo-picker [data-filter="' + name + '"]');
  }

  function apply() {
    var count = document.getElementById("repo-count");
    if (!box() || !count) return;
    var q = (filter("search").value || "").trim().toLowerCase();
    var owner = filter("owner").value;
    var visibility = filter("visibility").value;
    var forks = filter("forks").checked;
    var archived = filter("archived").checked;
    var all = rows(), shown = 0, selected = 0;
    all.forEach(function (tr) {
      var d = tr.dataset;
      var ok = (!q || d.search.indexOf(q) !== -1) &&
               (!owner || d.owner === owner) &&
               (!visibility || d.visibility === visibility) &&
               (forks || d.fork !== "1") &&
               (archived || d.archived !== "1");
      tr.hidden = !ok;
      if (ok) shown += 1;
      if (tr.querySelector("input[type=checkbox]").checked) selected += 1;
    });
    count.textContent = selected + " of " + all.length + " selected" +
      (shown < all.length ? " · " + shown + " shown" : "");
  }

  function inBox(ev) { return ev.target.closest && ev.target.closest("#repo-picker"); }
  document.addEventListener("input", function (ev) { if (inBox(ev)) apply(); });
  document.addEventListener("change", function (ev) { if (inBox(ev)) apply(); });
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest("#repo-picker [data-select]");
    if (!btn) return;
    var on = btn.dataset.select === "all";
    // Only the rows that the filters show: hidden rows keep their state.
    rows().forEach(function (tr) {
      if (!tr.hidden) tr.querySelector("input[type=checkbox]").checked = on;
    });
    apply();
  });
  document.addEventListener("htmx:afterSwap", apply);
  document.addEventListener("DOMContentLoaded", apply);
})();
