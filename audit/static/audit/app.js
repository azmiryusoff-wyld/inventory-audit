// Inventory Audit: page behaviour. Display only; every number comes from the server.
(function () {
  "use strict";

  // ---- Sortable tables and search boxes (unchanged behaviour) ----
  // Click a column heading of a .sortable table to sort it; a search box with data-filter="<table id>"
  // hides rows that don't contain the text, and updates any <tfoot> total marked data-sum.
  function value(td) {
    var t = (td ? td.getAttribute("data-v") || td.textContent : "").trim();
    var n = t.replace(/[,%\s]|RM/g, "");
    var neg = /^\(.*\)$/.test(n); n = n.replace(/[()]/g, "");
    if (n === "-" || n === "") return t === "" || t === "-" ? -Infinity : t.toLowerCase();
    var f = parseFloat(n);
    return isNaN(f) || !/^-?[\d.]+$/.test(n) ? t.toLowerCase() : (neg ? -f : f);
  }
  function fmt(v) {
    var a = Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
    return Math.abs(v) < 0.005 ? "-" : (v < 0 ? "(" + a + ")" : a);
  }
  function bodyRows(table) {
    return Array.prototype.filter.call(table.tBodies[0].rows, function (r) { return !r.querySelector("th") && !r.hasAttribute("data-empty"); });
  }
  // The column a footer cell sits under, counting cells that span several columns (e.g. "Total" colspan=4).
  function column(cell) {
    var i = 0, c = cell.previousElementSibling;
    while (c) { i += c.colSpan || 1; c = c.previousElementSibling; }
    return i;
  }
  function totals(table) {
    var foot = table.tFoot; if (!foot) return;
    Array.prototype.forEach.call(foot.querySelectorAll("[data-sum]"), function (cell) {
      var i = column(cell), sum = 0;
      bodyRows(table).forEach(function (r) { if (r.style.display !== "none") { var v = value(r.cells[i]); if (typeof v === "number" && isFinite(v)) sum += v; } });
      cell.textContent = fmt(sum);
    });
  }
  // Footer totals worked out in the browser (Trends) are filled in once the page loads.
  document.querySelectorAll("table[data-totals]").forEach(totals);
  document.querySelectorAll("table.sortable").forEach(function (table) {
    var head = table.tHead ? table.tHead.rows[0] : table.rows[0];
    Array.prototype.forEach.call(head.cells, function (th, i) {
      th.title = th.title || "Click to sort";
      th.addEventListener("click", function () {
        var dir = th.getAttribute("data-dir") === "asc" ? "desc" : "asc";
        Array.prototype.forEach.call(head.cells, function (c) { c.removeAttribute("data-dir"); });
        th.setAttribute("data-dir", dir);
        var rows = bodyRows(table);
        rows.sort(function (a, b) {
          var x = value(a.cells[i]), y = value(b.cells[i]);
          if (typeof x !== typeof y) { x = String(x); y = String(y); }
          return (x < y ? -1 : x > y ? 1 : 0) * (dir === "asc" ? 1 : -1);
        });
        rows.forEach(function (r) { r.parentNode.appendChild(r); });
      });
    });
  });
  document.querySelectorAll("[data-filter]").forEach(function (box) {
    var table = document.getElementById(box.getAttribute("data-filter"));
    var extra = box.getAttribute("data-filter-select") ? document.getElementById(box.getAttribute("data-filter-select")) : null;
    var count = document.getElementById(box.getAttribute("data-filter") + "-count");
    function apply() {
      var q = box.value.toLowerCase().trim(), cat = extra ? extra.value : "", shown = 0;
      bodyRows(table).forEach(function (r) {
        var ok = (!q || r.textContent.toLowerCase().indexOf(q) !== -1) && (!cat || r.getAttribute("data-group") === cat);
        r.style.display = ok ? "" : "none"; if (ok) shown++;
      });
      if (count) count.textContent = shown + " shown";
      totals(table);
    }
    box.addEventListener("input", apply); if (extra) extra.addEventListener("change", apply);
  });

  // ---- Sidebar on phones and tablets ----
  var toggle = document.getElementById("nav-toggle");
  function closeNav() { document.body.classList.remove("nav-open"); if (toggle) toggle.setAttribute("aria-expanded", "false"); }
  if (toggle) {
    toggle.addEventListener("click", function () {
      var open = document.body.classList.toggle("nav-open");
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    var scrim = document.querySelector(".scrim"); if (scrim) scrim.addEventListener("click", closeNav);
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeNav(); });
  }

  // ---- Notifications: close button ----
  document.querySelectorAll(".msg .close").forEach(function (b) {
    b.addEventListener("click", function () {
      var m = b.closest(".msg"); m.classList.add("hide"); setTimeout(function () { m.remove(); }, 200);
    });
  });

  // ---- Loading state: a pressed submit button shows a spinner and can't be pressed twice ----
  document.querySelectorAll("form").forEach(function (form) {
    if ((form.getAttribute("method") || "get").toLowerCase() !== "post") return;
    form.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;              // e.g. the person pressed Cancel on a confirm box
      var btn = e.submitter || form.querySelector("button:not([type=button])");
      if (!btn || btn.tagName !== "BUTTON") return;
      // Wait a tick so the browser has already taken the form's values.
      setTimeout(function () { btn.classList.add("is-loading"); btn.disabled = true; }, 0);
    });
  });
  // Coming back with the browser's Back button: buttons work again.
  window.addEventListener("pageshow", function (e) {
    if (!e.persisted) return;
    document.querySelectorAll("button.is-loading").forEach(function (b) { b.classList.remove("is-loading"); b.disabled = false; });
  });

  // ---- Bars in table cells: width relative to the biggest value in the same group ----
  var groups = {};
  document.querySelectorAll(".hbar[data-bar]").forEach(function (bar) {
    var g = bar.getAttribute("data-group") || "all", v = parseFloat(bar.getAttribute("data-bar"));
    if (isNaN(v)) v = 0;
    (groups[g] = groups[g] || []).push([bar, Math.abs(v)]);
  });
  Object.keys(groups).forEach(function (g) {
    var max = Math.max.apply(null, groups[g].map(function (p) { return p[1]; }));
    groups[g].forEach(function (p) {
      var w = max > 0 ? p[1] / max * 100 : 0;
      requestAnimationFrame(function () { p[0].style.width = (w > 0 && w < 1.5 ? 1.5 : w) + "%"; });
    });
  });

  // ---- Trends: variance % per audit, as diverging bars around zero ----
  var chart = document.getElementById("trends-chart"), source = document.getElementById("trends-table");
  if (chart && source) {
    var rows = Array.prototype.filter.call(source.tBodies[0].rows, function (r) { return r.hasAttribute("data-var"); });
    var tol = parseFloat(chart.getAttribute("data-tolerance")) || 0;
    var points = rows.slice(0, 24).map(function (r) {
      return { name: r.getAttribute("data-label"), v: parseFloat(r.getAttribute("data-var")), t: r.getAttribute("data-var-text"),
               tol: parseFloat(r.getAttribute("data-tol")) };
    }).filter(function (p) { return !isNaN(p.v); });
    if (!points.length) { chart.closest(".card").style.display = "none"; return; }
    var max = Math.max.apply(null, points.map(function (p) { return Math.abs(p.v); }).concat([tol * 1.5, 0.01]));
    points.forEach(function (p) {
      var row = document.createElement("div"); row.className = "row";
      var name = document.createElement("div"); name.className = "name"; name.textContent = p.name; name.title = p.name;
      var track = document.createElement("div"); track.className = "track";
      var t = isNaN(p.tol) ? tol : p.tol;
      if (t > 0) { var band = document.createElement("span"); band.className = "band";
        band.style.left = (50 - t / max * 50) + "%"; band.style.width = (t / max * 100) + "%"; track.appendChild(band); }
      var bar = document.createElement("i"); var w = Math.abs(p.v) / max * 50;
      bar.style.width = w + "%"; bar.style.left = p.v >= 0 ? "50%" : (50 - w) + "%";
      if (Math.abs(p.v) > t) bar.className = "over";
      track.appendChild(bar);
      var val = document.createElement("div"); val.className = "val"; val.textContent = p.t;
      row.appendChild(name); row.appendChild(track); row.appendChild(val); chart.appendChild(row);
    });
  }
})();
