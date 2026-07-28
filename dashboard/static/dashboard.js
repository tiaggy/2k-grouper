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

  let legendRendered = false;

  function fmtWeekShort(weekStartIso) {
    const d = new Date(weekStartIso + "T00:00:00");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
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

  function dayCell(code, dateIso, colors) {
    const td = document.createElement("td");
    td.className = "day-cell" + (isWeekend(dateIso) ? " weekend" : "");
    if (code) {
      td.classList.add("has-code");
      td.textContent = code;
      const c = colors[code];
      if (c) { td.style.background = c.fill; td.style.color = c.font; }
    }
    return td;
  }

  function buildTeamTable(node, team, colors) {
    const weekHeaderRow = node.querySelector(".week-header-row");
    const dowRow = node.querySelector(".dow-row");
    const tbody = node.querySelector("tbody");
    weekHeaderRow.innerHTML = "";
    dowRow.innerHTML = "";
    tbody.innerHTML = "";

    // Corner cells above the sticky worker-name column.
    const cornerTh = document.createElement("th");
    cornerTh.className = "corner-cell";
    cornerTh.rowSpan = 1;
    weekHeaderRow.appendChild(cornerTh);
    const cornerTh2 = document.createElement("th");
    cornerTh2.className = "corner-cell";
    dowRow.appendChild(cornerTh2);

    // Union of every worker across every week, sorted by name.
    const workerNames = new Map(); // name -> {username}
    for (const week of team.weeks) {
      for (const w of week.table.workers) workerNames.set(w.name, w.username);
    }
    const names = [...workerNames.keys()].sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));

    // Per-week lookup: name -> {date: code}
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
      th.dataset.groupId = team.group_id;
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "week-header-btn";
      btn.innerHTML = `<span class="wk-date">${fmtWeekShort(week.week_start)}</span>` +
        `<span class="wk-status">${week.approved ? "✓ Approved" : "Not approved"}</span>`;
      btn.title = week.approved ? "Click to un-approve this week" : "Click to approve this week";
      btn.addEventListener("click", () => submitApproval(team.group_id, week.week_start, !week.approved, btn));
      th.appendChild(btn);
      weekHeaderRow.appendChild(th);

      week.table.days.forEach((iso, i) => {
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
      td.textContent = "No records yet.";
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
      for (const { week, m } of weekLookup) {
        const days = m.get(name);
        week.table.days.forEach((iso, i) => {
          const td = dayCell(days ? days[iso] : null, iso, colors);
          if (i === 0) td.classList.add("week-start-border");
          tr.appendChild(td);
        });
      }
      tbody.appendChild(tr);
    }
  }

  function render(data) {
    renderLegend(data.legend, data.colors);

    // Preserve each team's horizontal scroll position across re-renders —
    // these tables can get very wide (a whole tracked history), and losing
    // scroll position on every ~15s poll would be jarring.
    const scrollByTeam = new Map();
    for (const el of teamsEl.querySelectorAll(".team")) {
      scrollByTeam.set(el.dataset.groupId, el.querySelector(".table-scroll").scrollLeft);
    }

    teamsEl.innerHTML = "";
    if (data.teams.length === 0) {
      teamsEl.innerHTML = '<p class="empty-note">No teams with attendance data yet.</p>';
      return;
    }
    for (const team of data.teams) {
      const node = tmplTeam.content.firstElementChild.cloneNode(true);
      node.dataset.groupId = team.group_id;
      node.querySelector(".team-label").textContent = team.label;
      buildTeamTable(node, team, data.colors);
      teamsEl.appendChild(node);
      const prevScroll = scrollByTeam.get(team.group_id);
      if (prevScroll) node.querySelector(".table-scroll").scrollLeft = prevScroll;
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

  fetchData();
  setInterval(fetchData, POLL_MS);
})();
