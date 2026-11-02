// The two doors (#40): the door's search box (filters the list beneath it on the empty page, suggests on a result
// page), the narrow-to pickers (only choices with rates; a notice with alternatives when what was typed has none),
// and Search in (applies as soon as it changes; "Find your plan" picks that plan's network). Everything shown comes from
// the JSON API as text, never as HTML.
(function () {
  "use strict";

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = text;
    return e;
  }
  function n(x) { return Number(x).toLocaleString("en-US"); }
  function plural(x, word, words) { return n(x) + " " + (x === 1 ? word : (words || word + "s")); }

  // What one option says, per kind of list: [key (bold, left), text, count (right)].
  var OPTION = {
    procedure: function (p) {
      return [p.billing_code, p.name + (p.billing_code_type === "RC" ? " (hospital department)" : ""),
              plural(p.rate_count, "rate")];
    },
    provider: function (p) {
      var count = plural(p.rate_count, "rate");
      if (p.hospital_count && p.hospital_count === p.rate_count) count += ", all hospital";
      else if (p.hospital_count) count += ", " + n(p.hospital_count) + " hospital";
      return [p.npi ? "NPI " + p.npi : "", p.name, "EIN " + p.ein + " · " + count];
    },
    code: function (c) {
      var count = plural(c.rate_count, "rate");
      if (c.hospital_count && c.hospital_count === c.rate_count) count += ", hospital only";
      return [c.billing_code, c.name, count];
    },
    providers: function (p) {
      return ["", (p.matched_names || p.names || [])[0] || "(no name)", "EIN " + p.ein + " · " + plural(p.rate_count, "rate")];
    },
    plan: function (p) {
      return [p.plan_id, p.plan_name, p.network === p.short_name ? p.short_name : p.network + " · " + p.short_name];
    }
  };
  function itemsOf(body) { return body.procedures || body.codes || body.providers || body.plans || []; }

  function fetchJSON(url, done) {
    fetch(url).then(function (r) { return r.ok ? r.json() : null; })
      .then(function (body) { done(body); }, function () { done(null); });
  }
  function withQuery(api, params) {
    var sep = api.indexOf("?") === -1 ? "?" : "&";
    return api + sep + Object.keys(params).map(function (k) {
      return encodeURIComponent(k) + "=" + encodeURIComponent(params[k]);
    }).join("&");
  }

  // A notice (api.pickers): message, then each alternative as a link.
  function renderNotice(slot, notice) {
    slot.innerHTML = "";
    if (!notice) return;
    var box = el("div", "notice dead");
    box.setAttribute("role", "status");
    box.appendChild(el("p", null, notice.message));
    if (notice.alternatives && notice.alternatives.length) {
      var alts = el("div", "alts");
      notice.alternatives.forEach(function (a) {
        var link = el("a", "alt", a.label);
        link.href = a.href;
        alts.appendChild(link);
      });
      box.appendChild(alts);
    }
    slot.appendChild(box);
  }

  // A dropdown under input: fetches source(q) as you type; Up / Down move, Enter or a click opens the option (go(item),
  // by default its href).
  function dropdown(input, kind, url, onBody, go) {
    go = go || function (item) { location.href = item.href; };
    var list = el("ul", "suggest");
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.parentNode.appendChild(list);
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    var items = [], active = -1, timer = null, asked = 0;
    function close() { list.hidden = true; active = -1; input.setAttribute("aria-expanded", "false"); }
    function render(body) {
      items = body ? itemsOf(body) : [];
      list.innerHTML = "";
      if (body && body.total > items.length && items.length) {
        list.appendChild(el("li", "sec", "Top " + items.length + " of " + n(body.total)));
      }
      items.forEach(function (item, i) {
        var parts = OPTION[kind](item), li = el("li");
        li.setAttribute("role", "option");
        if (parts[0]) li.appendChild(el("span", "v", parts[0]));
        li.appendChild(el("span", "d", parts[1]));
        li.appendChild(el("span", "l", parts[2]));
        li.addEventListener("mousedown", function (e) { e.preventDefault(); go(items[i]); });
        li.dataset.i = i;
        list.appendChild(li);
      });
      active = -1;
      list.hidden = !items.length;
      input.setAttribute("aria-expanded", String(!list.hidden));
      if (onBody) onBody(body);
    }
    function options() { return list.querySelectorAll("li[role=option]"); }
    function highlight(i) {
      active = i;
      options().forEach(function (li, j) { li.setAttribute("aria-selected", String(j === i)); });
      if (options()[i]) options()[i].scrollIntoView({ block: "nearest" });
    }
    function update() {
      var ticket = ++asked;
      fetchJSON(url(input.value.trim()), function (body) {
        if (ticket === asked && document.activeElement === input) render(body);
      });
    }
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(update, 150); });
    input.addEventListener("focus", update);
    input.addEventListener("blur", function () { setTimeout(close, 150); });
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();  // never submits: only real options navigate
        if (items.length) go(items[Math.max(active, 0)]);
        return;
      }
      if (list.hidden) { if (e.key === "ArrowDown") { update(); e.preventDefault(); } return; }
      if (e.key === "ArrowDown") { e.preventDefault(); highlight(Math.min(active + 1, items.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); highlight(Math.max(active - 1, 0)); }
      else if (e.key === "Escape") { close(); }
    });
  }

  // The empty page: the box filters the list beneath it (the server's rows are replaced as you type).
  var ROW = {
    procedure: function (p) {
      var hosp = p.billing_code_type === "RC";
      return [[p.billing_code, "code", true], [p.name], [hosp ? "hospital department" : p.billing_code_type, hosp ? "badge hosp" : ""],
              [n(p.rate_count), "num"], [n(p.ein_count), "num"]];
    },
    providers: function (p) {
      var names = p.matched_names && p.matched_names.length ? p.matched_names : p.names;
      var more = p.name_count - names.slice(0, 3).length;
      return [[names.slice(0, 3).join("; ") + (more > 0 ? " +" + more + " more" : ""), "", true], [p.ein, "code"],
              [n(p.npi_count), "num"], [n(p.code_count), "num"], [n(p.rate_count), "num"]];
    }
  };
  function liveList(input, kind, table) {
    var body = table.tBodies[0], count = document.querySelector("[data-count]"), more = document.querySelector("[data-more]");
    var timer = null, asked = 0, first = null;
    function render(data) {
      var rows = itemsOf(data);
      body.innerHTML = "";
      rows.forEach(function (item) {
        var tr = el("tr");
        tr.dataset.href = item.href;
        ROW[kind](item).forEach(function (cell) {
          var td = el("td", cell[1] && cell[1].indexOf("badge") === -1 ? cell[1] : "");
          if (cell[2]) { var a = el("a", null, cell[0]); a.href = item.href; td.appendChild(a); }
          else if (cell[1] && cell[1].indexOf("badge") !== -1) td.appendChild(el("span", cell[1], cell[0]));
          else td.textContent = cell[0];
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
      if (!rows.length) {
        var tr = el("tr"), td = el("td", "muted", data.notice ? data.notice.message : "Nothing matches “" + data.q + "”.");
        td.colSpan = table.tHead.rows[0].cells.length;
        tr.appendChild(td);
        body.appendChild(tr);
      }
      first = rows.length ? rows[0].href : null;
      if (count) {
        var all = n(count.dataset.all) + " " + count.dataset.noun;
        if (data.order) count.dataset.order = data.order;
        count.textContent = (data.q ? n(data.total) + " of " + all + " match “" + data.q + "”" : "All " + all) +
          ", " + count.dataset.order + ". " + count.dataset.tail;
      }
      if (more) {
        more.hidden = data.total <= rows.length;
        more.textContent = "The first " + rows.length + " of " + n(data.total) + "; keep typing to narrow.";
      }
    }
    // The page's sort (a header's sort/dir; none on the procedure list is its own order), kept as you type. The URL
    // names it only once it was asked for, so the default stays a bare ?q=.
    var sort = table.dataset.sort || "", dir = table.dataset.dir || "", named = /[?&]sort=/.test(location.search);
    function pageQuery(q, extra) {
      var params = new URLSearchParams();
      if (q) params.set("q", q);
      Object.keys(extra || {}).forEach(function (k) { params.set(k, extra[k]); });
      return params.toString() ? "?" + params : "";
    }
    // The headers say the sort (aria-sort, the arrow) and link to the next one with what is typed (the link works
    // without script too).
    function heads(q) {
      table.tHead.querySelectorAll("th.sortable").forEach(function (th) {
        var active = th.dataset.col === sort, next = active ? (dir === "asc" ? "desc" : "asc") : th.dataset.first || "asc";
        th.setAttribute("aria-sort", active ? (dir === "asc" ? "ascending" : "descending") : "none");
        th.querySelector(".arrow").textContent = active ? (dir === "asc" ? "▲" : "▼") : "↕";
        th.querySelector("a").href = location.pathname + pageQuery(q, { sort: th.dataset.col, dir: next });
      });
    }
    function update(fallback) {
      var ticket = ++asked, q = input.value.trim(), api = new URL(input.dataset.door, location.href);
      api.searchParams.set("q", q);
      api.searchParams.set("limit", 100);
      if (sort) { api.searchParams.set("sort", sort); api.searchParams.set("dir", dir); }
      fetchJSON(api.href, function (data) {
        if (ticket !== asked) return;
        if (!data) { if (fallback) location.href = fallback; return; }
        render(data);
        heads(q);
        try { history.replaceState(null, "", location.pathname + pageQuery(q, named && sort ? { sort: sort, dir: dir } : null)); } catch (e) {}
      });
    }
    // A header click re-sorts in place; if the API can't answer, its link loads the sorted page.
    table.tHead.addEventListener("click", function (e) {
      var a = e.target.closest("th.sortable a");
      if (!a || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      var params = new URL(a.href).searchParams;
      sort = params.get("sort");
      dir = params.get("dir");
      named = true;
      update(a.href);
    });
    input.addEventListener("input", function () { clearTimeout(timer); timer = setTimeout(update, 120); });
    input.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      e.preventDefault();
      var row = body.querySelector("tr[data-href]");
      if (row) location.href = row.dataset.href;
    });
    if (input.value) { var end = input.value.length; input.setSelectionRange(end, end); }
  }

  document.querySelectorAll("input[data-door]").forEach(function (input) {
    var kind = input.dataset.kind, table = input.dataset.list && document.getElementById(input.dataset.list);
    if (table) {
      liveList(input, kind, table);
    } else {
      dropdown(input, kind, function (q) { return withQuery(input.dataset.door, { q: q, limit: 8 }); });
    }
  });

  // Narrow-to pickers: options with rates only; the notice slot says when what was typed has none.
  document.querySelectorAll("input[data-picker]").forEach(function (input) {
    var slot = input.closest(".narrow").querySelector(".notice-slot");
    dropdown(input, input.dataset.kind, function (q) { return withQuery(input.dataset.picker, { q: q }); },
      function (body) { renderNotice(slot, body && input.value.trim() ? body.notice : null); });
  });

  // Rows of a list open their link when clicked anywhere.
  document.addEventListener("click", function (e) {
    var row = e.target.closest("tr[data-href]");
    if (row && !e.target.closest("a, button, input, details")) location.href = row.dataset.href;
  });

  // Search in applies as soon as it changes; all networks ("") stays out of the URL.
  function searchIn(form, value) {
    form.querySelectorAll("input[name=in]").forEach(function (r) { r.disabled = true; });
    if (value) {
      var pick = el("input");
      pick.type = "hidden"; pick.name = "in"; pick.value = value; pick.dataset.picked = "";
      form.appendChild(pick);
    }
    form.submit();
  }
  document.querySelectorAll("input[data-autosubmit]").forEach(function (radio) {
    radio.addEventListener("change", function () { if (radio.checked) searchIn(radio.form, radio.value); });
  });
  // Find your plan: picking one searches its network (in=plan-<id>, so the page says which network it is in).
  document.querySelectorAll("input[data-plans]").forEach(function (input) {
    dropdown(input, "plan", function (q) { return withQuery(input.dataset.plans, { q: q }); }, null,
      function (plan) { searchIn(input.form, plan.value); });
  });
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("input[data-autosubmit]").forEach(function (r) { r.disabled = false; });
    document.querySelectorAll("input[data-picked]").forEach(function (h) { h.remove(); });
  });
})();
