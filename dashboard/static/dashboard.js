(() => {
  "use strict";

  const POLL_MS = 15000;
  const TOKEN_KEY = "dashboardApproveToken";
  const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  const teamsEl = document.getElementById("teams");
  const legendEl = document.getElementById("legend");
  const statusPill = document.getElementById("status-pill");
  const generatedAtEl = document.getElementById("generated-at");
  const yearSelect = document.getElementById("year-select");
  const tmplTeam = document.getElementById("tmpl-team");

  let legendRendered = false;
  let selectedYear = null;   // string, e.g. "2026" — set once data first arrives
  let centeredYear = null;   // which year we've already auto-centered on load

  function fmtMonth(dateIso) {
    return new Date(dateIso + "T00:00:00").toLocaleDateString(undefined, { month: "short" });
  }
  function fmtDay(dateIso) {
    return new Date(dateIso + "T00:00:00").getDate();
  }
  function isWeekend(dateIso) {
    const dow = new Date(dateIso + "T00:00:00").getDay(); // 0=Sun..6=Sat
    return dow === 0 || dow === 6;
  }

  function renderLegend(legend, colors) {
    if (legendRendered) return;
    legendRendered = true;
    legendEl.innerHTML = "";
    for (const [code, label] of legend) {
      const item = document.createElement("span");
      item.className = "legend-item";
      const sw = document.createElement("span");
      sw.className = "legend-swatch";
      sw.textContent = code;
      sw.style.background = colors[code].fill;
      sw.style.color = colors[code].font;
      item.appendChild(sw);
      item.appendChild(document.createTextNode(label));
      legendEl.appendChild(item);
    }
  }

  function populateYearSelect(years) {
    const prev = yearSelect.value;
    yearSelect.innerHTML = "";
    for (const y of years) {
      const opt = document.createElement("option");
      opt.value = String(y);
      opt.textContent = String(y);
      yearSelect.appendChild(opt);
    }
    if (selectedYear && years.map(String).includes(selectedYear)) {
      yearSelect.value = selectedYear;
    } else if (prev && years.map(String).includes(prev)) {
      yearSelect.value = prev;
      selectedYear = prev;
    } else {
      // Default to the year containing today, if present, else the last (most recent) option.
      const todayYear = String(new Date().getFullYear());
      selectedYear = years.map(String).includes(todayYear) ? todayYear : String(years[years.length - 1]);
      yearSelect.value = selectedYear;
    }
  }

  function dayCell(code, dateIso, colors, editCtx, legend, preStart) {
    const td = document.createElement("td");
    td.className = "day-cell" + (isWeekend(dateIso) || preStart ? " weekend" : "");
    if (code) {
      td.classList.add("has-code");
      td.textContent = code;
      const c = colors[code];
      if (c) { td.style.background = c.fill; td.style.color = c.font; }
    }
    if (editCtx) {
      td.classList.add("editable");
      td.title = "Click to set this day's status";
      td.addEventListener("click", (e) => {
        e.stopPropagation();
        openEditPicker(td, { groupId: editCtx.groupId, accountId: editCtx.accountId, dateIso, code }, legend, colors);
      });
    }
    return td;
  }

  function buildTeamTable(node, team, colors, legend) {
    const weekHeaderRow = node.querySelector(".week-header-row");
    const daynumRow = node.querySelector(".daynum-row");
    const dowRow = node.querySelector(".dow-row");
    const tbody = node.querySelector("tbody");
    weekHeaderRow.innerHTML = "";
    daynumRow.innerHTML = "";
    dowRow.innerHTML = "";
    tbody.innerHTML = "";

    for (const row of [weekHeaderRow, daynumRow, dowRow]) {
      const corner = document.createElement("th");
      corner.className = "corner-cell";
      row.appendChild(corner);
    }

    // team.weeks arrives chronologically ascending (oldest -> newest -> future) already.
    // Row order: earliest first-recorded day (within this year) first, then
    // alphabetically among workers who started the same day.
    const firstDate = new Map(); // name -> earliest date ISO seen this year
    const accountIdByName = new Map();
    for (const week of team.weeks) {
      for (const w of week.table.workers) {
        const days = Object.keys(w.days);
        if (days.length === 0) continue;
        const earliest = days.reduce((a, b) => (a < b ? a : b));
        const prev = firstDate.get(w.name);
        if (!prev || earliest < prev) firstDate.set(w.name, earliest);
        if (!accountIdByName.has(w.name)) accountIdByName.set(w.name, w.account_id);
      }
    }
    const names = [...firstDate.keys()].sort((a, b) => {
      const da = firstDate.get(a), db = firstDate.get(b);
      if (da !== db) return da < db ? -1 : 1;
      return a.toLowerCase().localeCompare(b.toLowerCase());
    });

    const weekLookup = team.weeks.map((week) => {
      const m = new Map();
      for (const w of week.table.workers) m.set(w.name, w.days);
      return { week, m };
    });

    for (const { week } of weekLookup) {
      const th = document.createElement("th");
      th.className = "week-header " + (week.approved ? "approved" : "not-approved");
      th.colSpan = 7;
      th.dataset.weekStart = week.week_start;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "week-header-btn";
      const startLabel = `${fmtMonth(week.week_start)} ${fmtDay(week.week_start)}`;
      const endLabel = `${fmtMonth(week.week_end)} ${fmtDay(week.week_end)}`;
      btn.innerHTML = `<span class="wk-date">${startLabel} – ${endLabel}</span>` +
        `<span class="wk-status">${week.approved ? "✓ Approved" : "Not approved"}</span>`;
      btn.title = week.approved ? "Click to un-approve this week" : "Click to approve this week";
      btn.addEventListener("click", () => submitApproval(team.group_id, week.week_start, !week.approved, btn));
      th.appendChild(btn);
      weekHeaderRow.appendChild(th);

      week.table.days.forEach((iso, i) => {
        const numTh = document.createElement("th");
        numTh.className = "daynum-cell" + (i === 0 ? " week-start-border" : "");
        numTh.textContent = fmtDay(iso);
        daynumRow.appendChild(numTh);

        const dth = document.createElement("th");
        dth.className = "dow-cell" + (i === 0 ? " week-start-border" : "");
        dth.textContent = DOW[i];
        dowRow.appendChild(dth);
      });
    }

    if (names.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty-note";
      td.colSpan = 1 + team.weeks.length * 7;
      td.textContent = "No records this year.";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    for (const name of names) {
      const tr = document.createElement("tr");
      const nameTd = document.createElement("td");
      nameTd.className = "worker-name";
      nameTd.textContent = name;
      tr.appendChild(nameTd);
      const accountId = accountIdByName.get(name);
      const startIso = firstDate.get(name);
      for (const { week, m } of weekLookup) {
        const days = m.get(name);
        const editCtx = week.approved ? null : { groupId: team.group_id, accountId };
        week.table.days.forEach((iso, i) => {
          const preStart = !!startIso && iso < startIso;
          const td = dayCell(days ? days[iso] : null, iso, colors, editCtx, legend, preStart);
          if (i === 0) td.classList.add("week-start-border");
          tr.appendChild(td);
        });
      }
      tbody.appendChild(tr);
    }
  }

  function centerOnCurrentWeek(node, currentWeekStart) {
    if (!currentWeekStart) return;
    const th = node.querySelector(`.week-header[data-week-start="${currentWeekStart}"]`);
    const scroller = node.querySelector(".table-scroll");
    if (!th || !scroller) return;
    const target = th.offsetLeft - scroller.clientWidth / 2 + th.offsetWidth / 2;
    scroller.scrollLeft = Math.max(0, target);
  }

  function render(data) {
    closePicker(); // the table gets rebuilt below; don't leave a picker pointing at a stale cell
    renderLegend(data.legend, data.colors);
    populateYearSelect(data.available_years);
    const yearData = data.years[selectedYear];
    if (!yearData) return;

    // teamsEl.innerHTML = "" below wipes the whole subtree, which can reset
    // the page's own vertical scroll depending on how the browser reflows —
    // capture it now and restore it once the rebuild is done.
    const pageScrollY = window.scrollY;
    const scrollByTeam = new Map();
    for (const el of teamsEl.querySelectorAll(".team")) {
      scrollByTeam.set(el.dataset.groupId, el.querySelector(".table-scroll").scrollLeft);
    }
    const shouldCenter = centeredYear !== selectedYear;

    teamsEl.innerHTML = "";
    if (yearData.teams.length === 0) {
      teamsEl.innerHTML = '<p class="empty-note">No teams with attendance data in ' + selectedYear + '.</p>';
      centeredYear = selectedYear;
      window.scrollTo(0, pageScrollY);
      return;
    }
    for (const team of yearData.teams) {
      const node = tmplTeam.content.firstElementChild.cloneNode(true);
      node.dataset.groupId = team.group_id;
      node.querySelector(".team-label").textContent = team.label;
      buildTeamTable(node, team, data.colors, data.legend);
      teamsEl.appendChild(node);
      if (shouldCenter) {
        centerOnCurrentWeek(node, yearData.current_week_start);
      } else {
        const prevScroll = scrollByTeam.get(team.group_id);
        if (prevScroll) node.querySelector(".table-scroll").scrollLeft = prevScroll;
      }
    }
    centeredYear = selectedYear;
    window.scrollTo(0, pageScrollY);
  }

  function setStatus(kind, text) {
    statusPill.className = "status-pill status-" + kind;
    statusPill.textContent = text;
  }

  let lastData = null;

  async function fetchData() {
    try {
      const token = localStorage.getItem(TOKEN_KEY) || "";
      const res = await fetch("/api/data", { cache: "no-store", headers: { "X-Approve-Token": token } });
      if (res.status === 401) {
        setStatus("error", "login required");
        showTokenBanner(fetchData);
        return;
      }
      if (res.status === 403) {
        setStatus("error", "dashboard not configured on the server (no token set)");
        return;
      }
      const data = await res.json();
      if (!res.ok) {
        setStatus("error", data.error || "error");
        return;
      }
      lastData = data;
      render(data);
      setStatus(data.stale_error ? "error" : "ok", data.stale_error ? "last refresh failed — showing cached data" : "live");
      generatedAtEl.textContent = "updated " + new Date(data.generated_at).toLocaleTimeString();
    } catch (err) {
      setStatus("error", "unreachable");
    }
  }

  yearSelect.addEventListener("change", () => {
    selectedYear = yearSelect.value;
    if (lastData) render(lastData);   // instant — already have every year's data
  });

  function showTokenBanner(retryFn) {
    // fetchData polls every 15s and would otherwise recreate this (stealing
    // focus, dropping whatever's half-typed) on every single failed poll
    // while the token prompt is still up — leave an existing one alone.
    if (document.querySelector(".token-banner")) return;
    const banner = document.createElement("div");
    banner.className = "token-banner";
    banner.innerHTML = `
      <span>Approve token:</span>
      <input type="password" placeholder="token" autocomplete="off">
      <button type="button">Save &amp; retry</button>
    `;
    const input = banner.querySelector("input");
    const btn = banner.querySelector("button");
    const submit = () => {
      const v = input.value.trim();
      if (!v) return;
      localStorage.setItem(TOKEN_KEY, v);
      banner.remove();
      retryFn();
    };
    btn.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    document.body.appendChild(banner);
    input.focus();
  }

  async function submitApproval(groupId, weekStart, approved, btnEl) {
    btnEl.disabled = true;
    try {
      const token = localStorage.getItem(TOKEN_KEY) || "";
      const res = await fetch("/api/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Approve-Token": token },
        body: JSON.stringify({ group_id: groupId, week_start: weekStart, approved }),
      });
      if (res.status === 401) {
        showTokenBanner(() => submitApproval(groupId, weekStart, approved, btnEl));
        return;
      }
      if (res.status === 403) {
        alert("Approving is disabled on this server (no token configured).");
        return;
      }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        alert("Failed: " + (d.detail || res.status));
        return;
      }
      await fetchData();
    } finally {
      btnEl.disabled = false;
    }
  }

  let openPicker = null;

  function closePicker() {
    if (openPicker) { openPicker.remove(); openPicker = null; }
    document.removeEventListener("click", onDocClickClosePicker, true);
    document.removeEventListener("keydown", onDocKeyClosePicker, true);
  }
  function onDocClickClosePicker(e) {
    if (openPicker && !openPicker.contains(e.target)) closePicker();
  }
  function onDocKeyClosePicker(e) {
    if (e.key === "Escape") closePicker();
  }

  function openEditPicker(cellEl, ctx, legend, colors) {
    closePicker();
    const picker = document.createElement("div");
    picker.className = "edit-picker";
    for (const [code, label] of legend) {
      const opt = document.createElement("button");
      opt.type = "button";
      opt.className = "edit-picker-opt" + (code === ctx.code ? " current" : "");
      const sw = document.createElement("span");
      sw.className = "edit-picker-swatch";
      sw.textContent = code;
      sw.style.background = colors[code].fill;
      sw.style.color = colors[code].font;
      opt.appendChild(sw);
      opt.appendChild(document.createTextNode(label));
      opt.addEventListener("click", () => { closePicker(); submitEdit(ctx, code); });
      picker.appendChild(opt);
    }
    const clearOpt = document.createElement("button");
    clearOpt.type = "button";
    clearOpt.className = "edit-picker-opt edit-picker-clear";
    clearOpt.textContent = "— No record (clear) —";
    clearOpt.addEventListener("click", () => { closePicker(); submitEdit(ctx, ""); });
    picker.appendChild(clearOpt);

    document.body.appendChild(picker);
    const r = cellEl.getBoundingClientRect();
    const pr = picker.getBoundingClientRect();
    let left = r.left + window.scrollX;
    const maxLeft = window.scrollX + window.innerWidth - pr.width - 8;
    if (left > maxLeft) left = Math.max(8, maxLeft);
    let top = r.bottom + window.scrollY + 4;
    if (top + pr.height > window.scrollY + window.innerHeight) {
      top = r.top + window.scrollY - pr.height - 4;
    }
    picker.style.left = left + "px";
    picker.style.top = top + "px";
    openPicker = picker;

    setTimeout(() => {
      document.addEventListener("click", onDocClickClosePicker, true);
      document.addEventListener("keydown", onDocKeyClosePicker, true);
    }, 0);
  }

  async function submitEdit(ctx, code) {
    try {
      const token = localStorage.getItem(TOKEN_KEY) || "";
      const res = await fetch("/api/edit-day", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Approve-Token": token },
        body: JSON.stringify({ group_id: ctx.groupId, account_id: ctx.accountId, date: ctx.dateIso, code }),
      });
      if (res.status === 401) {
        showTokenBanner(() => submitEdit(ctx, code));
        return;
      }
      if (res.status === 403) {
        alert("Editing is disabled on this server (no token configured).");
        return;
      }
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        alert("Failed: " + (d.detail || res.status));
        return;
      }
      await fetchData();
    } catch (err) {
      alert("Failed: " + err);
    }
  }

  fetchData();
  setInterval(fetchData, POLL_MS);
})();
