/* ==========================================================================
   Every data table: sort by any column, and search the rows on the page.

   Tables split across pages sort and search on the server instead, because here
   the browser only has one page of rows. Those headers are links (.th-sort).

   Opt out with data-no-sort (statements) or data-no-search (the page has its own).
   ========================================================================== */
(function () {
  "use strict";

  var MIN_ROWS_FOR_SEARCH = 6;
  var MONTHS = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12 };
  var EMPTY = /^(—|-|–|·|n\/a)?$/i;

  /** Understands how this app prints money, dates, times and durations. */
  function parse(raw) {
    var text = raw.replace(/\s+/g, " ").trim();
    if (EMPTY.test(text)) return { kind: "empty", value: 0 };
    var match = text.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/);
    if (match) return { kind: "date", value: +match[1] * 1e8 + +match[2] * 1e6 + +match[3] * 1e4 + (+match[4] || 0) * 100 + (+match[5] || 0) };
    match = text.match(/^(\d{1,2}) ([A-Za-z]{3})[a-z]*(?: (\d{4}))?/);
    if (match && MONTHS[match[2].toLowerCase()]) {
      return { kind: "date", value: (+match[3] || 0) * 1e8 + MONTHS[match[2].toLowerCase()] * 1e6 + +match[1] * 1e4 };
    }
    match = text.match(/^(\d{1,2}):(\d{2})/);
    if (match) return { kind: "time", value: +match[1] * 60 + +match[2] };
    match = text.match(/^(\d+)h(?: (\d+)m)?/) ;
    if (match) return { kind: "number", value: +match[1] * 60 + (+match[2] || 0) };
    match = text.match(/^(?:₹|Rs\.?)?\s*(−|-)?\s*(?:₹|Rs\.?)?\s*(\d[\d,]*(?:\.\d+)?)/);
    if (match && /^[^A-Za-z]*\d[\d,.]*\s*(%|m|d|days?|h|hours?|min|minutes?|yrs?|years?)?(\b|$|\s)/.test(text)) {
      var value = parseFloat(match[2].replace(/,/g, ""));
      return { kind: "number", value: match[1] ? -value : value };
    }
    return { kind: "text", value: text.toLowerCase() };
  }

  function cellText(cell) {
    if (!cell) return "";
    if (cell.dataset.sort !== undefined) return cell.dataset.sort;
    // The bold part is the value: a name, not the initials in the avatar beside it.
    var main = cell.querySelector("strong");
    return main ? main.textContent : cell.textContent;
  }

  /** A row's helpers (edit forms, notes, details) belong to the row above them. */
  function groups(table) {
    var head = table.tHead && table.tHead.rows[0];
    var width = head ? head.cells.length : 0;
    var body = table.tBodies[0];
    var result = [];
    Array.prototype.forEach.call(body ? body.rows : [], function (row) {
      var isHelper = row.classList.contains("detail-row") || row.classList.contains("m-detail") ||
        row.classList.contains("m-only") || row.id || row.cells.length !== width;
      if (isHelper && result.length) {
        result[result.length - 1].rows.push(row);
      } else if (!isHelper) {
        result.push({ main: row, rows: [row], pinned: row.classList.contains("total-row") });
      }
    });
    return result;
  }

  function sortBy(table, index, button) {
    var head = table.tHead.rows[0];
    var th = head.cells[index];
    var ascending = th.getAttribute("aria-sort") !== "ascending";
    Array.prototype.forEach.call(head.cells, function (cell) { cell.removeAttribute("aria-sort"); });
    th.setAttribute("aria-sort", ascending ? "ascending" : "descending");

    var list = groups(table);
    var parsed = list.map(function (group, order) {
      return { group: group, order: order, key: parse(cellText(group.main.cells[index])) };
    });
    var kinds = {};
    parsed.forEach(function (item) { if (item.key.kind !== "empty") kinds[item.key.kind] = true; });
    var sameKind = Object.keys(kinds).length <= 1;

    parsed.sort(function (a, b) {
      if (a.group.pinned !== b.group.pinned) return a.group.pinned ? 1 : -1;
      // Blank values stay last in both directions.
      if ((a.key.kind === "empty") !== (b.key.kind === "empty")) return a.key.kind === "empty" ? 1 : -1;
      var result;
      if (sameKind && a.key.kind !== "text") {
        result = a.key.value - b.key.value;
      } else {
        result = cellText(a.group.main.cells[index]).trim().localeCompare(
          cellText(b.group.main.cells[index]).trim(), undefined, { numeric: true, sensitivity: "base" });
      }
      if (!result) return a.order - b.order;
      return ascending ? result : -result;
    });
    var body = table.tBodies[0];
    parsed.forEach(function (item) {
      item.group.rows.forEach(function (row) { body.appendChild(row); });
    });
    if (button) button.focus({ preventScroll: true });
  }

  function makeHeadersSortable(table) {
    var head = table.tHead.rows[0];
    Array.prototype.forEach.call(head.cells, function (th, index) {
      var label = th.textContent.trim();
      if (!label || th.classList.contains("check-cell")) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "th-sort";
      button.title = "Sort by " + label.replace(/\s+/g, " ");
      while (th.firstChild) button.appendChild(th.firstChild);
      th.appendChild(button);
      button.addEventListener("click", function () { sortBy(table, index, button); });
    });
  }

  /** Phones hide the header of card-style tables, so they get a sort menu instead. */
  function mobileSortMenu(table, bar, serverSide) {
    if (!table.classList.contains("stack")) return;
    var select = document.createElement("select");
    select.className = "table-sort-menu";
    select.setAttribute("aria-label", "Sort this table");
    var first = document.createElement("option");
    first.value = "";
    first.textContent = "Sort by…";
    select.appendChild(first);
    Array.prototype.forEach.call(table.tHead.rows[0].cells, function (th, index) {
      var label = th.textContent.replace(/\s+/g, " ").trim();
      if (!label || th.classList.contains("check-cell")) return;
      var option = document.createElement("option");
      var link = th.querySelector("a.th-sort");
      option.value = serverSide && link ? link.getAttribute("href") : String(index);
      option.textContent = label;
      select.appendChild(option);
    });
    select.addEventListener("change", function () {
      if (!select.value) return;
      if (serverSide) { window.location.href = select.value; return; }
      sortBy(table, parseInt(select.value, 10));
    });
    bar.appendChild(select);
  }

  function searchBox(table, bar) {
    var input = document.createElement("input");
    input.type = "search";
    input.className = "table-search";
    input.placeholder = "Search this table…";
    input.setAttribute("aria-label", "Search this table");
    var count = document.createElement("span");
    count.className = "table-count small muted";
    count.setAttribute("role", "status");
    input.addEventListener("input", function () {
      var words = input.value.toLowerCase().split(/\s+/).filter(Boolean);
      var list = groups(table);
      var shown = 0;
      list.forEach(function (group) {
        var text = group.main.textContent.toLowerCase();
        var match = words.every(function (word) { return text.indexOf(word) !== -1; });
        if (match) shown += 1;
        group.rows.forEach(function (row) { row.style.display = match ? "" : "none"; });
      });
      count.textContent = words.length ? shown + " of " + list.length : "";
    });
    bar.appendChild(input);
    bar.appendChild(count);
  }

  function init(table) {
    if (table.hasAttribute("data-no-sort") || table.classList.contains("statement") ||
        table.classList.contains("compact") || !table.tHead || !table.tHead.rows.length || !table.tBodies.length) return;
    var serverSide = table.hasAttribute("data-server-sort");
    var count = serverSide ? 0 : groups(table).length;
    var isRegister = table.classList.contains("register");
    if (!serverSide) makeHeadersSortable(table);

    var bar = document.createElement("div");
    bar.className = "table-tools no-print";
    if (!isRegister && !serverSide && !table.hasAttribute("data-no-search") && count >= MIN_ROWS_FOR_SEARCH) searchBox(table, bar);
    if (!bar.children.length) bar.classList.add("phone-only");
    mobileSortMenu(table, bar, serverSide);
    if (bar.children.length) {
      var wrap = table.closest(".table-wrap") || table;
      wrap.parentNode.insertBefore(bar, wrap);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("table.data, table.register").forEach(init);
  });
})();
