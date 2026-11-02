// A procedure's prices (#26, #40): popovers, the group picker filter, clickable group rows, the Filters toggle,
// suggestion dropdowns for the filter boxes, and drag-to-select on the distribution chart (#18).
(function () {
  "use strict";

  // Popovers are <details class="pop">: one open at a time, closed by a click elsewhere or Escape.
  var pops = document.querySelectorAll("details.pop");
  function closePops(except) {
    pops.forEach(function (p) { if (p !== except) p.open = false; });
  }
  pops.forEach(function (p) {
    p.addEventListener("toggle", function () {
      if (!p.open) return;
      closePops(p);
      var box = p.querySelector(".group-filter");
      if (box) box.focus();
    });
  });
  document.addEventListener("click", function (e) {
    pops.forEach(function (p) { if (p.open && !p.contains(e.target)) p.open = false; });
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") closePops(null); });

  // Group picker: type to narrow the list.
  document.querySelectorAll(".group-filter").forEach(function (box) {
    var options = box.closest(".popover").querySelectorAll(".group-option");
    box.addEventListener("input", function () {
      var q = box.value.trim().toLowerCase();
      options.forEach(function (o) { o.hidden = q && o.textContent.toLowerCase().indexOf(q) === -1; });
    });
    box.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      var first = Array.prototype.find.call(options, function (o) { return !o.hidden; });
      if (first) location.href = first.href;
    });
  });

  // Compare Groups: a click anywhere on a row picks that group and opens Statistics.
  document.querySelectorAll("tr.group-row[data-href]").forEach(function (row) {
    row.addEventListener("click", function (e) {
      if (e.target.closest("a")) return;
      location.href = row.dataset.href;
    });
  });
  document.querySelectorAll("[data-show-groups]").forEach(function (button) {
    button.addEventListener("click", function () {
      document.querySelectorAll("tbody.more-groups").forEach(function (t) { t.hidden = false; });
      button.parentNode.hidden = true;
    });
  });

  // Rates: the per-column filter row is behind the Filters button.
  var toggle = document.getElementById("filters-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var row = document.getElementById(toggle.getAttribute("aria-controls"));
      row.hidden = !row.hidden;
      toggle.setAttribute("aria-pressed", String(!row.hidden));
    });
  }

  // Suggestion dropdowns for the filter row: a list under the box, filled from values embedded in the page
  // (data-options). Up / Down move, Enter or a click picks, Escape closes.
  function combo(input, source) {
    var list = document.createElement("ul");
    list.className = "suggest";
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.parentNode.appendChild(list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    var items = [], active = -1, timer = null, asked = 0;

    function close() { list.hidden = true; active = -1; input.setAttribute("aria-expanded", "false"); }
    function render(found) {
      items = found;
      list.innerHTML = "";
      found.forEach(function (s, i) {
        var li = document.createElement("li");
        li.setAttribute("role", "option");
        var v = document.createElement("span");
        v.className = "v";
        v.textContent = s.value;
        li.appendChild(v);
        if (s.label) {
          var l = document.createElement("span");
          l.className = "l";
          l.textContent = s.label + (s.names > 1 ? " +" + (s.names - 1) : "");
          li.appendChild(l);
        }
        li.addEventListener("mousedown", function (e) { e.preventDefault(); pick(i); });
        list.appendChild(li);
      });
      active = -1;
      list.hidden = !found.length;
      input.setAttribute("aria-expanded", String(!list.hidden));
    }
    function highlight(i) {
      active = i;
      Array.prototype.forEach.call(list.children, function (li, j) { li.setAttribute("aria-selected", String(j === i)); });
      if (list.children[i]) list.children[i].scrollIntoView({ block: "nearest" });
    }
    function pick(i) {
      input.value = items[i].value;
      close();
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    function update() {
      var ticket = ++asked;
      source(input.value.trim(), function (found) { if (ticket === asked && document.activeElement === input) render(found); });
    }
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(update, 180); });
    input.addEventListener("focus", update);
    input.addEventListener("blur", function () { setTimeout(close, 100); });
    input.addEventListener("keydown", function (e) {
      if (list.hidden) { if (e.key === "ArrowDown") { update(); e.preventDefault(); } return; }
      if (e.key === "ArrowDown") { e.preventDefault(); highlight(Math.min(active + 1, items.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); highlight(Math.max(active - 1, 0)); }
      else if (e.key === "Enter" && active >= 0) { e.preventDefault(); e.stopImmediatePropagation(); pick(active); }
      else if (e.key === "Escape") { e.stopPropagation(); close(); }
    }, true);
  }

  document.querySelectorAll("input[data-options]").forEach(function (input) {
    var values = [];
    try { values = JSON.parse(document.getElementById(input.dataset.options).textContent); } catch (e) {}
    combo(input, function (q, done) {
      var needle = q.toLowerCase();
      done(values.filter(function (v) { return v.toLowerCase().indexOf(needle) !== -1; }).slice(0, 50)
        .map(function (v) { return { value: v }; }));
    });
  });

  // Distribution chart (#18): drag across it to show only that price range (sets rate_min / rate_max and
  // reloads); a click without a drag does nothing.
  document.querySelectorAll(".chartbox[data-href]").forEach(function (box) {
    var svg = box.querySelector("svg"), brush = box.querySelector(".brush");
    if (!svg) return;
    var min = +box.dataset.min, max = +box.dataset.max, width = +box.dataset.width;
    var left = +box.dataset.left, right = +box.dataset.right;
    var start = null;
    function clientToValue(clientX) {
      var r = svg.getBoundingClientRect();
      var x = (clientX - r.left) / r.width * width;
      return Math.max(min, Math.min(max, min + (x - left) / (right - left) * (max - min)));
    }
    function show(a, b) {
      var r = svg.getBoundingClientRect(), outer = box.getBoundingClientRect();
      var px = function (v) { return r.left - outer.left + (left + (v - min) / (max - min) * (right - left)) / width * r.width; };
      brush.style.left = px(a) + "px";
      brush.style.width = Math.max(1, px(b) - px(a)) + "px";
      brush.hidden = false;
    }
    var text = box.parentNode.querySelector(".sel-text");
    function money(v) { return "$" + v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
    svg.addEventListener("pointerdown", function (e) {
      if (e.button !== 0) return;
      start = { x: e.clientX, v: clientToValue(e.clientX) };
      svg.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
    svg.addEventListener("pointermove", function (e) {
      if (!start) return;
      var v = clientToValue(e.clientX);
      show(Math.min(start.v, v), Math.max(start.v, v));
      if (text) text.textContent = "Release to show " + money(Math.min(start.v, v)) + " – " + money(Math.max(start.v, v)) + " only";
    });
    svg.addEventListener("pointerup", function (e) {
      if (!start) return;
      var v = clientToValue(e.clientX), moved = Math.abs(e.clientX - start.x) > 4;
      var lo = Math.floor(Math.min(start.v, v) * 100) / 100, hi = Math.ceil(Math.max(start.v, v) * 100) / 100;
      start = null;
      if (!moved || hi <= lo) { brush.hidden = true; return; }
      location.href = box.dataset.href + "&rate_min=" + lo.toFixed(2) + "&rate_max=" + hi.toFixed(2);
    });
  });
})();
