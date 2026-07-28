(() => {
  "use strict";

  const POLL_MS = 15000;
  const TOKEN_KEY = "dashboardApproveToken";
  const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

  const teamsEl = document.getElementById("teams");
  const legendEl = document.getElementById("legend");
  const statusPill = document.getElementById("status-pill");
  const generatedAtEl = document.getElementById("generated-at");
  const tmplTeam = document.getElementById("tmpl-team");
  const tmplWeek = document.getElementById("tmpl-week");

  // key = `${group_id}|${week_start}` -> {approved, expanded}
  const weekUi = new Map();
  let legendRendered = false;
  let pendingApprovePayload = null;

  function fmtWeek(weekStartIso) {
    const d = new Date(weekStartIso + "T00:00:00");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
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

  function weekKey(groupId, weekStart) {
    return `${groupId}|${weekStart}`;
  }

  function uiFor(groupId, week) {
    const key = weekKey(groupId, week.week_start);
    let ui = weekUi.get(key);
    if (!ui || ui.approved !== week.approved) {
      ui = { approved: week.approved, expanded: !week.approved };
      weekUi.set(key, ui);
    }
    return ui;
  }

  function buildWeekTable(table, colors, weekendFill) {
    const scroll = document.createElement("div");
    scroll.className = "table-scroll";
    const tableEl = document.createElement("table");
    tableEl.className = "week-table";

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    headRow.appendChild(document.createElement("th"));
    table.days.forEach((iso, i) => {
      const th = document.createElement("th");
      th.textContent = DOW[i];
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    tableEl.appendChild(thead);

    const tbody = document.createElement("tbody");
    if (table.workers.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = table.days.length + 1;
      td.className = "empty-note";
      td.textContent = "No records this week.";
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    for (const worker of table.workers) {
      const tr = document.createElement("tr");
      const nameTd = document.createElement("td");
      nameTd.className = "worker-name";
      nameTd.textContent = worker.name;
      tr.appendChild(nameTd);
      for (const iso of table.days) {
        const td = document.createElement("td");
        const code = worker.days[iso];
        td.className = "day-cell" + (isWeekend(iso) ? " weekend" : "");
        if (code) {
          td.classList.add("has-code");
          td.textContent = code;
          const c = colors[code];
          if (c) { td.style.background = c.fill; td.style.color = c.font; }
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    tableEl.appendChild(tbody);
    scroll.appendChild(tableEl);
    return scroll;
  }

  function renderWeek(groupId, groupLabel, week, colors, weekendFill) {
    const node = tmplWeek.content.firstElementChild.cloneNode(true);
    const ui = uiFor(groupId, week);
    node.classList.toggle("collapsed", !ui.expanded);
    node.dataset.groupId = groupId;
    node.dataset.weekStart = week.week_start;

    node.querySelector(".week-title").textContent = `Week of ${fmtWeek(week.week_start)}`;
    const badge = node.querySelector(".approve-badge");
    badge.textContent = week.approved ? "Approved" : "Not approved";
    badge.className = "approve-badge " + (week.approved ? "approved" : "not-approved");

    const approveBtn = node.querySelector(".approve-btn");
    approveBtn.textContent = week.approved ? "Un-approve" : "Approve";
    approveBtn.classList.toggle("is-approved", week.approved);

    node.querySelector(".week-bar").addEventListener("click", () => {
      ui.expanded = !ui.expanded;
      node.classList.toggle("collapsed", !ui.expanded);
    });

    approveBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      submitApproval(groupId, groupLabel, week.week_start, !week.approved, approveBtn);
    });

    node.querySelector(".week-body").appendChild(buildWeekTable(week.table, colors, weekendFill));
    return node;
  }

  function render(data) {
    renderLegend(data.legend, data.colors);
    teamsEl.innerHTML = "";
    if (data.teams.length === 0) {
      teamsEl.innerHTML = '<p class="empty-note">No teams with attendance data yet.</p>';
      return;
    }
    for (const team of data.teams) {
      const teamNode = tmplTeam.content.firstElementChild.cloneNode(true);
      teamNode.querySelector(".team-label").textContent = team.label;
      const weeksEl = teamNode.querySelector(".weeks");
      for (const week of team.weeks) {
        weeksEl.appendChild(renderWeek(team.group_id, team.label, week, data.colors, data.weekend_fill));
      }
      teamsEl.appendChild(teamNode);
    }
  }

  function setStatus(kind, text) {
    statusPill.className = "status-pill status-" + kind;
    statusPill.textContent = text;
  }

  async function fetchData() {
    try {
      const res = await fetch("/api/data", { cache: "no-store" });
      const data = await res.json();
      if (!res.ok) {
        setStatus("error", data.error || "error");
        return;
      }
      render(data);
      setStatus(data.stale_error ? "error" : "ok", data.stale_error ? "last refresh failed — showing cached data" : "live");
      generatedAtEl.textContent = "updated " + new Date(data.generated_at).toLocaleTimeString();
    } catch (err) {
      setStatus("error", "unreachable");
    }
  }

  function showTokenBanner(retryFn) {
    let banner = document.querySelector(".token-banner");
    if (banner) banner.remove();
    banner = document.createElement("div");
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

  async function submitApproval(groupId, groupLabel, weekStart, approved, btnEl) {
    btnEl.disabled = true;
    try {
      const token = localStorage.getItem(TOKEN_KEY) || "";
      const res = await fetch("/api/approve", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Approve-Token": token },
        body: JSON.stringify({ group_id: groupId, week_start: weekStart, approved }),
      });
      if (res.status === 401) {
        showTokenBanner(() => submitApproval(groupId, groupLabel, weekStart, approved, btnEl));
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
      weekUi.set(weekKey(groupId, weekStart), { approved, expanded: !approved });
      await fetchData();
    } finally {
      btnEl.disabled = false;
    }
  }

  fetchData();
  setInterval(fetchData, POLL_MS);
})();
