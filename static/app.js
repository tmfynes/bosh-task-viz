(function () {
  "use strict";

  const FAMILY_LABEL = {
    prepare: "prepare",
    compile: "compile",
    create_vm: "create vm",
    update_instance: "update instance",
    drain_stop: "drain / stop",
    packages: "packages",
    start: "start",
    error: "error",
    other: "other",
  };
  const FAMILY_ORDER = ["prepare", "compile", "create_vm", "update_instance", "drain_stop", "packages", "start", "error", "other"];

  const state = {
    tasks: [],
    selectedId: null,
    detailCache: new Map(), // id -> last /api/tasks/<id> response
    ssePaused: false,
    drawerPaused: false,
    lastUpdateTs: null,
  };

  const el = (id) => document.getElementById(id);
  const railEl = el("rail");
  const tabsEl = el("tabs");
  const tooltipEl = el("tooltip");
  const drawerLogEl = el("drawer-log");
  const drawerCountEl = el("drawer-count");
  const drawerPausedEl = el("drawer-paused");

  function familyColor(family) {
    return getComputedStyle(document.documentElement).getPropertyValue(`--family-${family}`) || "#8aa0b2";
  }

  function fmtDuration(ms) {
    if (ms == null || ms < 0) return "–";
    const totalSec = Math.floor(ms / 1000);
    const h = Math.floor(totalSec / 3600);
    const m = Math.floor((totalSec % 3600) / 60);
    const s = totalSec % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
  }

  function fmtAge(sec) {
    if (sec == null) return "–";
    if (sec < 2) return "just now";
    if (sec < 60) return `${Math.floor(sec)}s ago`;
    return `${Math.floor(sec / 60)}m ago`;
  }

  function stateClass(taskState) {
    if (taskState === "processing") return "processing";
    if (taskState === "error" || taskState === "errored" || taskState === "cancelled" || taskState === "timeout") return "error";
    if (taskState === "queued") return "queued";
    return "done";
  }

  // -- networking -----------------------------------------------------------

  async function postJson(path) {
    const r = await fetch(path, { method: "POST" });
    return r.json();
  }

  async function fetchTasks() {
    try {
      const r = await fetch("/api/tasks");
      const data = await r.json();
      state.tasks = data.tasks || [];
      state.lastUpdateTs = Date.now() / 1000;
      ensureSelection();
      renderRail();
      renderTabs();
      renderHeaderStats();
    } catch (e) {
      // network hiccup; next poll will retry
    }
  }

  async function fetchHealth() {
    try {
      const r = await fetch("/api/health");
      const data = await r.json();
      applyHealth(data);
    } catch (e) {
      applyHealth({ ssh: { connected: false, error: String(e) }, bosh: { ok: false, error: String(e) } });
    }
  }

  async function fetchDetail(id) {
    try {
      const r = await fetch(`/api/tasks/${id}`);
      if (!r.ok) return null;
      const data = await r.json();
      state.detailCache.set(id, data);
      if (id === state.selectedId) {
        renderGantt(data);
        renderDrawer(data);
        renderGanttHeader(data);
      }
      return data;
    } catch (e) {
      return null;
    }
  }

  // -- selection --------------------------------------------------------------

  function ensureSelection() {
    const ids = state.tasks.map((t) => t.id);
    if (state.selectedId != null && ids.includes(state.selectedId)) return;
    const processing = state.tasks.find((t) => t.state === "processing");
    if (processing) {
      selectTask(processing.id, { silent: true });
      return;
    }
    if (state.tasks.length) {
      selectTask(state.tasks[0].id, { silent: true });
    }
  }

  function selectTask(id, opts) {
    opts = opts || {};
    state.selectedId = id;
    if (!opts.silent) {
      renderRail();
      renderTabs();
    }
    fetchDetail(id);
  }

  // -- header -----------------------------------------------------------------

  function applyHealth(data) {
    lastHealth = data || {};
    const demoBadge = el("demo-badge");
    demoBadge.hidden = !data.demo;

    const sshPill = el("pill-ssh");
    const sshText = el("pill-ssh-text");
    const ssh = data.ssh || {};
    sshPill.className = "pill " + (ssh.connected ? "ok" : data.demo ? "warn" : "bad");
    sshText.textContent = ssh.connected
      ? `ssh ${ssh.user || ""}@${ssh.host || ""}`
      : data.demo
      ? "ssh demo mode"
      : `ssh down${ssh.error ? ": " + truncate(ssh.error, 60) : ""}`;

    const boshPill = el("pill-bosh");
    const boshText = el("pill-bosh-text");
    const bosh = data.bosh || {};
    boshPill.className = "pill " + (bosh.ok ? "ok" : "bad");
    boshText.textContent = bosh.ok ? `bosh ${bosh.name || "ok"}` : `bosh ${bosh.error ? truncate(bosh.error, 40) : "unknown"}`;
  }

  function truncate(s, n) {
    if (!s) return s;
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }

  let lastHealth = {};
  function sshEmptyHint() {
    const ssh = lastHealth.ssh || {};
    if (lastHealth.demo) return "";
    if (ssh.connected) return "";
    return (
      "Ops Manager SSH is down. Bind a user-provided service named ops-manager-ssh " +
      "with host, ip, port, user, private_key (cf cups ops-manager-ssh -p creds.json). " +
      (ssh.error ? "Last error: " + ssh.error : "")
    );
  }

  function renderHeaderStats() {
    const processing = state.tasks.filter((t) => t.state === "processing").length;
    el("stat-processing").textContent = String(processing);
    const ageSec = state.lastUpdateTs ? Date.now() / 1000 - state.lastUpdateTs : null;
    el("stat-age").textContent = fmtAge(ageSec);
  }

  // -- left rail ----------------------------------------------------------------

  function renderRail() {
    railEl.innerHTML = "";
    const processing = state.tasks.filter((t) => t.state === "processing");
    const recent = state.tasks.filter((t) => t.state !== "processing");

    if (processing.length) {
      const h = document.createElement("h2");
      h.textContent = "processing";
      railEl.appendChild(h);
      processing.forEach((t) => railEl.appendChild(taskRow(t)));
    }

    const h2 = document.createElement("h2");
    h2.textContent = "recent";
    railEl.appendChild(h2);
    if (recent.length) {
      recent.forEach((t) => railEl.appendChild(taskRow(t)));
    } else {
      const empty = document.createElement("div");
      empty.className = "rail-empty";
      empty.textContent = "no other tasks";
      railEl.appendChild(empty);
    }
  }

  function taskRow(t) {
    const row = document.createElement("div");
    row.className = "task-item" + (t.id === state.selectedId ? " selected" : "");
    row.onclick = () => selectTask(t.id);

    const dot = document.createElement("span");
    dot.className = "task-state-dot " + stateClass(t.state);
    row.appendChild(dot);

    const body = document.createElement("div");
    body.className = "task-item-body";
    const title = document.createElement("div");
    title.className = "task-item-title";
    title.textContent = `#${t.id} ${t.description || t.deployment || ""}`;
    const sub = document.createElement("div");
    sub.className = "task-item-sub";
    const bits = [t.state];
    if (t.elapsed_s != null) bits.push(fmtDuration(t.elapsed_s * 1000));
    if (t.current_chore) bits.push(t.current_chore);
    sub.textContent = bits.filter(Boolean).join(" · ");
    body.appendChild(title);
    body.appendChild(sub);
    row.appendChild(body);

    const pin = document.createElement("button");
    pin.className = "pin-btn" + (t.tracked ? " pinned" : "");
    pin.title = t.tracked ? "untrack" : "track";
    pin.textContent = "✦";
    pin.onclick = (ev) => {
      ev.stopPropagation();
      togglePin(t);
    };
    row.appendChild(pin);

    return row;
  }

  async function togglePin(t) {
    const path = `/api/tasks/${t.id}/${t.tracked ? "untrack" : "track"}`;
    await postJson(path);
    fetchTasks();
  }

  // -- tabs (multiple processing tasks) ----------------------------------------

  function renderTabs() {
    const processing = state.tasks.filter((t) => t.state === "processing" || t.tracked);
    tabsEl.innerHTML = "";
    if (processing.length < 2) return;
    processing.forEach((t) => {
      const tab = document.createElement("div");
      tab.className = "tab" + (t.id === state.selectedId ? " active" : "");
      tab.textContent = `#${t.id}`;
      tab.onclick = () => selectTask(t.id);
      tabsEl.appendChild(tab);
    });
  }

  // -- gantt header -------------------------------------------------------------

  function renderGanttHeader(detail) {
    const meta = detail.meta || {};
    el("gantt-title").textContent = `#${meta.id} ${meta.description || ""}`;
    const bits = [meta.deployment, meta.state, meta.user].filter(Boolean);
    el("gantt-meta").textContent = bits.join(" · ");
  }

  function renderLegend() {
    const legend = el("gantt-legend");
    legend.innerHTML = "";
    FAMILY_ORDER.forEach((fam) => {
      const span = document.createElement("span");
      const sw = document.createElement("span");
      sw.className = "legend-swatch";
      sw.style.background = `var(--family-${fam})`;
      span.appendChild(sw);
      span.appendChild(document.createTextNode(FAMILY_LABEL[fam]));
      legend.appendChild(span);
    });
  }
  renderLegend();

  // -- gantt (D3) -----------------------------------------------------------------

  const MARGIN = { top: 10, right: 30, bottom: 30, left: 220 };
  const ROW_HEIGHT = 40;
  const PARENT_BAR_H = 16;
  const SUB_BAR_H = 10;

  function laneLabel(chore) {
    if (chore.task && chore.task !== chore.stage) return chore.task;
    return chore.stage || "(unknown)";
  }

  function renderGantt(detail) {
    const emptyEl = el("gantt-empty");
    const chores = (detail.chores || []).filter((c) => c.start != null);
    const svg = d3.select("#gantt");

    if (!chores.length) {
      svg.selectAll("*").remove();
      emptyEl.hidden = false;
      emptyEl.textContent = sshEmptyHint() || "no chores reported for this task yet";
      return;
    }
    emptyEl.hidden = true;

    const taskStart = d3.min(chores, (c) => c.start);
    const now = Date.now() / 1000;
    const taskEnd = d3.max(chores, (c) => (c.end != null ? c.end : now));
    const domainMax = Math.max(taskEnd - taskStart, 1);

    const lanes = [];
    const laneIndex = new Map();
    chores.forEach((c) => {
      const label = laneLabel(c);
      if (!laneIndex.has(label)) {
        laneIndex.set(label, lanes.length);
        lanes.push(label);
      }
    });

    const width = Math.max(el("gantt-scroll").clientWidth - 4, 600);
    const height = MARGIN.top + MARGIN.bottom + lanes.length * ROW_HEIGHT;
    svg.attr("width", width).attr("height", height);

    const innerWidth = width - MARGIN.left - MARGIN.right;
    const x = d3.scaleLinear().domain([0, domainMax]).range([0, innerWidth]);
    const y = d3.scaleBand().domain(lanes).range([0, lanes.length * ROW_HEIGHT]).padding(0.25);

    let g = svg.select("g.plot");
    if (g.empty()) {
      g = svg.append("g").attr("class", "plot");
      g.append("g").attr("class", "axis axis-x");
      g.append("g").attr("class", "axis axis-y");
      g.append("g").attr("class", "lanes");
      g.append("g").attr("class", "bars");
    }
    g.attr("transform", `translate(${MARGIN.left},${MARGIN.top})`);

    const xAxis = d3.axisBottom(x).ticks(8).tickFormat((d) => fmtDuration(d * 1000));
    g.select(".axis-x").attr("transform", `translate(0,${lanes.length * ROW_HEIGHT})`).transition().duration(400).call(xAxis);

    const yAxis = d3.axisLeft(y).tickSize(0);
    g.select(".axis-y").call(yAxis).selectAll("text").attr("class", "lane-label");
    g.select(".axis-y").select(".domain").remove();

    // lane background stripes
    const stripes = g.select(".lanes").selectAll("rect.stripe").data(lanes, (d) => d);
    stripes
      .join(
        (enter) =>
          enter
            .append("rect")
            .attr("class", "stripe")
            .attr("x", 0)
            .attr("width", innerWidth)
            .attr("y", (d) => y(d))
            .attr("height", y.bandwidth())
            .attr("fill", (_d, i) => (i % 2 === 0 ? "rgba(255,255,255,.02)" : "transparent")),
        (update) => update.attr("y", (d) => y(d)).attr("width", innerWidth).attr("height", y.bandwidth()),
        (exit) => exit.remove()
      );

    const barsG = g.select(".bars");

    // parent bars
    const parents = barsG.selectAll("rect.chore-bar.parent").data(chores, (c) => c.id);
    parents
      .join(
        (enter) =>
          enter
            .append("rect")
            .attr("class", (c) => `chore-bar parent state-${c.state}`)
            .attr("x", (c) => x(c.start - taskStart))
            .attr("y", (c) => y(laneLabel(c)) + (y.bandwidth() - PARENT_BAR_H) / 2)
            .attr("height", PARENT_BAR_H)
            .attr("rx", 3)
            .attr("fill", (c) => `var(--family-${c.family})`)
            .attr("opacity", 0.85)
            .attr("width", 0)
            .on("mousemove", (event, c) => showTooltip(event, choreTooltip(c)))
            .on("mouseleave", hideTooltip)
            .call((enter) =>
              enter
                .transition()
                .duration(400)
                .attr("width", (c) => Math.max(x((c.end != null ? c.end : now) - taskStart) - x(c.start - taskStart), 1))
            ),
        (update) =>
          update
            .attr("class", (c) => `chore-bar parent state-${c.state}`)
            .call((update) =>
              update
                .transition()
                .duration(400)
                .attr("x", (c) => x(c.start - taskStart))
                .attr("width", (c) => Math.max(x((c.end != null ? c.end : now) - taskStart) - x(c.start - taskStart), 1))
            ),
        (exit) => exit.transition().duration(200).attr("opacity", 0).remove()
      )
      .on("mousemove", (event, c) => showTooltip(event, choreTooltip(c)))
      .on("mouseleave", hideTooltip);

    // sub-chore bars (flattened)
    const subData = [];
    chores.forEach((c) => {
      (c.sub_chores || []).forEach((s, i) => {
        subData.push({ ...s, parent: c, key: `${c.id}-${i}-${s.name}-${s.start}` });
      });
    });

    const subs = barsG.selectAll("rect.chore-bar.sub").data(subData, (d) => d.key);
    subs
      .join(
        (enter) =>
          enter
            .append("rect")
            .attr("class", "chore-bar sub")
            .attr("x", (d) => x(d.start - taskStart))
            .attr("y", (d) => y(laneLabel(d.parent)) + (y.bandwidth() + PARENT_BAR_H) / 2 - 2)
            .attr("height", SUB_BAR_H)
            .attr("rx", 2)
            .attr("fill", (d) => `var(--family-${d.family})`)
            .attr("opacity", 0.65)
            .attr("width", 0)
            .on("mousemove", (event, d) => showTooltip(event, subTooltip(d)))
            .on("mouseleave", hideTooltip)
            .call((enter) =>
              enter
                .transition()
                .duration(400)
                .attr("width", (d) => Math.max(x((d.end != null ? d.end : now) - taskStart) - x(d.start - taskStart), 1))
            ),
        (update) =>
          update.call((update) =>
            update
              .transition()
              .duration(400)
              .attr("x", (d) => x(d.start - taskStart))
              .attr("width", (d) => Math.max(x((d.end != null ? d.end : now) - taskStart) - x(d.start - taskStart), 1))
          ),
        (exit) => exit.remove()
      )
      .on("mousemove", (event, d) => showTooltip(event, subTooltip(d)))
      .on("mouseleave", hideTooltip);
  }

  function choreTooltip(c) {
    const dur = c.duration_ms;
    return {
      title: laneLabel(c),
      rows: [
        `start: ${new Date(c.start * 1000).toLocaleTimeString()}`,
        `duration: ${fmtDuration(dur)}`,
        `progress: ${c.progress != null ? c.progress + "%" : "–"}`,
        `state: ${c.state}`,
      ],
    };
  }

  function subTooltip(d) {
    return {
      title: d.name,
      rows: [
        `parent: ${laneLabel(d.parent)}`,
        `start: ${new Date(d.start * 1000).toLocaleTimeString()}`,
        `duration: ${fmtDuration(d.duration_ms)}`,
      ],
    };
  }

  function showTooltip(event, { title, rows }) {
    tooltipEl.innerHTML = `<div class="t-title">${title}</div>` + rows.map((r) => `<div class="t-row">${r}</div>`).join("");
    tooltipEl.style.display = "block";
    tooltipEl.style.left = event.clientX + 14 + "px";
    tooltipEl.style.top = event.clientY + 14 + "px";
  }

  function hideTooltip() {
    tooltipEl.style.display = "none";
  }

  // -- debug drawer ---------------------------------------------------------------

  let drawerHover = false;
  drawerLogEl.addEventListener("mouseenter", () => (drawerHover = true));
  drawerLogEl.addEventListener("mouseleave", () => (drawerHover = false));

  function renderDrawer(detail) {
    if (drawerHover) return;
    const lines = detail.debug_lines || [];
    drawerCountEl.textContent = `${lines.length} lines`;
    const atBottom = drawerLogEl.scrollTop + drawerLogEl.clientHeight >= drawerLogEl.scrollHeight - 8;
    drawerLogEl.innerHTML = lines
      .map((l) => {
        const ts = l.ts ? `<span class="ts">${l.ts.slice(11, 23)}</span>` : "";
        const msg = l.logger ? `${l.logger}: ${l.message}` : l.message;
        return `<div class="line lvl-${l.level}">${ts}${escapeHtml(msg)}</div>`;
      })
      .join("");
    if (atBottom) drawerLogEl.scrollTop = drawerLogEl.scrollHeight;
  }

  function escapeHtml(s) {
    return (s || "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  }

  // -- SSE -------------------------------------------------------------------------

  function connectStream() {
    const es = new EventSource("/api/stream");
    es.addEventListener("snapshot", (ev) => {
      let payload;
      try {
        payload = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      state.tasks = payload.tasks || [];
      state.lastUpdateTs = payload.ts;
      applyHealth({ ssh: payload.ssh, bosh: payload.bosh, demo: payload.demo });
      ensureSelection();
      renderRail();
      renderTabs();
      renderHeaderStats();

      if (state.selectedId != null && payload.chores && payload.chores[state.selectedId] != null) {
        const cached = state.detailCache.get(state.selectedId) || { meta: {}, debug_lines: [] };
        const merged = { ...cached, chores: payload.chores[state.selectedId] };
        state.detailCache.set(state.selectedId, merged);
        renderGantt(merged);
      }
    });
    es.onerror = () => {
      // EventSource auto-reconnects; REST polling below covers gaps meanwhile.
    };
  }

  // -- boot -------------------------------------------------------------------------

  function tick() {
    renderHeaderStats();
    if (state.selectedId != null) fetchDetail(state.selectedId);
  }

  fetchHealth();
  fetchTasks();
  connectStream();
  setInterval(fetchTasks, 2000);
  setInterval(fetchHealth, 5000);
  setInterval(tick, 1500);
  setInterval(renderHeaderStats, 1000);
})();
