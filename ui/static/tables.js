// Show/hide table columns. Choices are remembered per table in this browser only.
document.querySelectorAll("details.columns").forEach(function (menu) {
  var table = document.getElementById(menu.dataset.table);
  if (!table) return;
  var key = "hidden-columns:" + menu.dataset.table;
  var hidden = [];
  try { hidden = JSON.parse(localStorage.getItem(key)) || []; } catch (e) {}
  function apply() {
    table.querySelectorAll("[data-col]").forEach(function (cell) {
      cell.hidden = hidden.indexOf(cell.dataset.col) !== -1;
    });
    try { localStorage.setItem(key, JSON.stringify(hidden)); } catch (e) {}
  }
  var list = menu.querySelector(".column-list");
  // One checkbox per column, from the header row only (not the filter row under it).
  table.querySelectorAll("thead tr:first-child th[data-col]").forEach(function (th) {
    var label = document.createElement("label");
    var box = document.createElement("input");
    box.type = "checkbox";
    box.checked = hidden.indexOf(th.dataset.col) === -1;
    box.addEventListener("change", function () {
      hidden = hidden.filter(function (c) { return c !== th.dataset.col; });
      if (!box.checked) hidden.push(th.dataset.col);
      apply();
    });
    label.append(box, " " + th.textContent.replace(/[▲▼↕]/g, "").trim());
    list.append(label);
  });
  var reset = document.createElement("button");
  reset.type = "button";
  reset.textContent = "Show all";
  reset.addEventListener("click", function () {
    hidden = [];
    list.querySelectorAll("input").forEach(function (b) { b.checked = true; });
    apply();
  });
  list.append(reset);
  apply();
  // Close the menu when clicking anywhere else.
  document.addEventListener("click", function (event) {
    if (menu.open && !menu.contains(event.target)) menu.open = false;
  });
});

// Drag a header's right edge to resize its column. Widths are remembered per table in this browser.
document.querySelectorAll("table[id]").forEach(function (table) {
  var headers = table.querySelectorAll("thead tr:first-child th[data-col]");
  if (!headers.length) return;
  var key = "column-widths:" + table.id;
  var widths = {};
  try { widths = JSON.parse(localStorage.getItem(key)) || {}; } catch (e) {}
  function setWidth(th, px) {
    th.style.width = th.style.minWidth = th.style.maxWidth = px + "px";
  }
  headers.forEach(function (th) {
    if (widths[th.dataset.col]) setWidth(th, widths[th.dataset.col]);
    var grip = document.createElement("span");
    grip.className = "col-resize";
    grip.title = "Drag to resize";
    grip.addEventListener("click", function (event) { event.preventDefault(); event.stopPropagation(); });
    grip.addEventListener("dblclick", function () {  // double-click: back to automatic width
      th.style.width = th.style.minWidth = th.style.maxWidth = "";
      delete widths[th.dataset.col];
      try { localStorage.setItem(key, JSON.stringify(widths)); } catch (e) {}
    });
    grip.addEventListener("pointerdown", function (event) {
      event.preventDefault();
      var startX = event.clientX, startWidth = th.getBoundingClientRect().width;
      grip.setPointerCapture(event.pointerId);
      document.body.classList.add("resizing");
      function move(e) { setWidth(th, Math.max(48, Math.round(startWidth + e.clientX - startX))); }
      function up() {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", up);
        document.body.classList.remove("resizing");
        widths[th.dataset.col] = Math.round(th.getBoundingClientRect().width);
        try { localStorage.setItem(key, JSON.stringify(widths)); } catch (e) {}
      }
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", up);
    });
    th.append(grip);
  });
});

// Filters apply on their own: dropdowns as soon as a value is picked, text boxes a moment after typing
// stops (or on Enter). Changing an operator applies it only when its column already has a value.
// The page reloads with the results, so the box being typed in gets its focus and cursor back.
(function () {
  var form = document.getElementById("filters");
  if (!form) return;
  var FOCUS_KEY = "filter-focus";
  var VALUELESS = /^(not_)?empty$/;
  function opFor(field) { return form.ownerDocument.querySelector('[form=filters][name="' + field.name + '_op"]'); }
  var timer = null;
  function submit(field) {
    clearTimeout(timer);
    try {
      if (field && field.tagName === "INPUT") sessionStorage.setItem(FOCUS_KEY, field.name);
      else sessionStorage.removeItem(FOCUS_KEY);
    } catch (e) {}
    // Leave empty filters (and their operators) out of the URL. "is empty" / "is not empty" need no value:
    // keep the operator and drop the value.
    document.querySelectorAll("[form=filters]").forEach(function (f) {
      if (f.type === "hidden") return;
      var isOp = f.classList.contains("filter-op");
      var op = isOp ? f : opFor(f);
      var value = isOp ? form.elements.namedItem(f.name.replace(/_op$/, "")) : f;
      var valueless = op && VALUELESS.test(op.value);
      if (isOp ? (!valueless && value && !value.value.trim()) : (valueless || !f.value.trim())) f.disabled = true;
    });
    form.requestSubmit();
  }
  document.querySelectorAll("[form=filters]").forEach(function (field) {
    if (field.classList.contains("filter-op")) field.dataset.was = field.value;
    if (field.tagName === "SELECT") {
      field.addEventListener("change", function () {
        if (field.classList.contains("filter-op")) {
          // Without a value an operator does nothing, except is empty / is not empty (or leaving one of them).
          var value = form.elements.namedItem(field.name.replace(/_op$/, ""));
          var was = field.dataset.was || "";
          field.dataset.was = field.value;
          if (!VALUELESS.test(field.value) && !VALUELESS.test(was) && (!value || !value.value.trim())) return;
        }
        submit(field);
      });
    } else if (field.type !== "hidden") {
      var last = field.value;
      field.addEventListener("input", function () {
        clearTimeout(timer);
        timer = setTimeout(function () { if (field.value !== last) submit(field); }, 600);
      });
      field.addEventListener("change", function () { if (field.value !== last) submit(field); });  // datalist pick
      field.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); submit(field); } });
    }
  });
  var name = null;
  try { name = sessionStorage.getItem(FOCUS_KEY); sessionStorage.removeItem(FOCUS_KEY); } catch (e) {}
  var refocus = name && form.ownerDocument.querySelector('[form=filters][name="' + name + '"]');
  if (refocus) { refocus.focus(); var end = refocus.value.length; try { refocus.setSelectionRange(end, end); } catch (e) {} }
})();

