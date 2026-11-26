// Small page helpers: copy to clipboard, "select all" and the checked-row
// count on the Repositories page. No framework. Everything is delegated from
// <body>, so it also works for rows that HTMX loads later.
(function () {
  "use strict";

  // --- copy to clipboard ---------------------------------------------------
  // navigator.clipboard needs HTTPS or localhost. A LAN address over HTTP has
  // no clipboard API, so fall back to select + execCommand.
  function copyText(text, input) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      input.focus();
      input.select();
      try {
        if (document.execCommand("copy")) { resolve(); } else { reject(); }
      } catch (err) {
        reject(err);
      }
    });
  }

  document.addEventListener("click", function (ev) {
    var button = ev.target.closest("[data-copy]");
    if (!button) return;
    var input = document.querySelector(button.getAttribute("data-copy"));
    if (!input) return;
    var label = button.getAttribute("data-label") || button.textContent;
    button.setAttribute("data-label", label);
    copyText(input.value, input).then(function () {
      button.textContent = "Copied";
    }, function () {
      // The text is selected now: the user can press Ctrl+C.
      input.focus();
      input.select();
      button.textContent = "Press Ctrl+C";
    });
    setTimeout(function () { button.textContent = label; }, 2000);
  });

  // --- row selection -------------------------------------------------------
  function rowBoxes() {
    return Array.prototype.slice.call(document.querySelectorAll("#repo-list input[name='ids']"));
  }

  function refreshSelection() {
    var boxes = rowBoxes();
    var checked = boxes.filter(function (b) { return b.checked; }).length;
    var counter = document.querySelector("[data-selected-count]");
    if (counter) counter.textContent = checked ? " (" + checked + ")" : "";
    var all = document.querySelector("[data-select-all]");
    if (all) {
      all.checked = boxes.length > 0 && checked === boxes.length;
      all.indeterminate = checked > 0 && checked < boxes.length;
    }
  }

  document.addEventListener("change", function (ev) {
    if (ev.target.matches("[data-select-all]")) {
      rowBoxes().forEach(function (b) { b.checked = ev.target.checked; });
    }
    if (ev.target.matches("[data-select-all], #repo-list input[name='ids']")) {
      refreshSelection();
    }
  });

  // The list is replaced by the filter, by a bulk action and by the 2-second
  // poll while a job runs. Keep the checked rows across that replacement.
  var kept = [];
  document.addEventListener("htmx:beforeSwap", function (ev) {
    if (ev.detail.target && ev.detail.target.id === "repo-list") {
      kept = rowBoxes().filter(function (b) { return b.checked; })
                       .map(function (b) { return b.value; });
    }
  });
  document.addEventListener("htmx:afterSwap", function () {
    if (kept.length) {
      rowBoxes().forEach(function (b) {
        if (kept.indexOf(b.value) !== -1) b.checked = true;
      });
      kept = [];
    }
    refreshSelection();
  });
})();
