/*
 * FlowTrack Orchestrator - minimal kanban UI.
 *
 * No build step, no framework. Polls /api/kanban every 5s for truth, listens
 * to /ws for live updates between polls. When a WS event references a task or
 * instance, it triggers a debounced refetch — DB is always source of truth.
 *
 * Replace with Next.js + TanStack Query in a separate repo when this grows.
 * See docs/ORCHESTRATOR.md §8.
 */

const COLUMNS = [
  ["discovery",   "Discovery"],
  ["refinement",  "Refinement"],
  ["ready",       "Ready"],
  ["in_progress", "In Progress"],
  ["blocked",     "Blocked"],
  ["in_review",   "In Review"],
  ["qa",          "QA"],
  ["merged",      "Merged"],
];

const $ = (id) => document.getElementById(id);

// Optional bearer token. Picked up from ?token=... in the URL on first load
// and stashed in sessionStorage so refreshes don't lose it. Cleared by
// reloading the page with ?token= (empty).
const _qsToken = new URLSearchParams(location.search).get("token");
if (_qsToken !== null) {
  if (_qsToken === "") sessionStorage.removeItem("flowtrack_token");
  else sessionStorage.setItem("flowtrack_token", _qsToken);
}
const TOKEN = sessionStorage.getItem("flowtrack_token") || "";

function authedFetch(url, opts = {}) {
  if (!TOKEN) return fetch(url, opts);
  const headers = new Headers(opts.headers || {});
  headers.set("Authorization", `Bearer ${TOKEN}`);
  return fetch(url, { ...opts, headers });
}

const state = {
  board: null,
  instanceSummary: new Map(), // instance_id -> latest event summary text
};

// ---------- rendering ----------

function renderBoard(board) {
  const root = $("board");
  root.innerHTML = "";

  for (const [key, label] of COLUMNS) {
    const items = board[key] || [];
    const col = document.createElement("div");
    col.className = "col";
    col.dataset.col = key;
    col.innerHTML = `<h3>${label}<span class="count">${items.length}</span></h3>`;
    if (items.length === 0) {
      col.innerHTML += `<div class="empty">empty</div>`;
    } else {
      for (const item of items) {
        col.appendChild(key === "discovery" ? discoveryCard(item) : taskCard(item));
      }
    }
    root.appendChild(col);
  }
}

function taskCard(t) {
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.taskId = t.id;
  const ticket = t.ticket_id ? `<span class="ticket">${escape(t.ticket_id)}</span>` : "";
  const module = t.module_hint ? `<span class="module">@${escape(t.module_hint)}</span>` : "";
  const role = t.current_role_name
    ? `<div class="role-badge">${escape(t.current_role_name)}</div>` : "";
  el.innerHTML = `
    <div class="title">${escape(t.title)}</div>
    <div class="meta">
      ${ticket}
      <span class="priority ${t.priority}">${t.priority}</span>
      ${module}
    </div>
    ${role}
  `;
  return el;
}

function discoveryCard(d) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="title">${escape(d.title)}</div>
    <div class="meta">
      <span class="ticket">${escape(d.source)}</span>
      <span>${escape(d.kind)}</span>
      ${d.signal_score ? `<span>score ${d.signal_score}</span>` : ""}
    </div>
  `;
  return el;
}

function renderInstances(list) {
  const root = $("instances-list");
  $("instances-count").textContent = `${list.length} instance${list.length === 1 ? "" : "s"}`;
  const sectionCount = $("instances-section-count");
  if (sectionCount) sectionCount.textContent = list.length;
  root.innerHTML = "";
  if (list.length === 0) {
    root.innerHTML = `<li class="empty">no live instances</li>`;
    return;
  }
  for (const i of list) {
    const li = document.createElement("li");
    li.className = "instance-row";
    li.dataset.instanceId = i.id;
    const summary = state.instanceSummary.get(i.id) || "(no events yet)";
    li.innerHTML = `
      <div>
        <span class="status-dot ${i.status}"></span>
        <span class="role">${escape(i.role_name)}</span>
        ${i.task_title ? `<span class="task">- ${escape(i.task_title)}</span>` : ""}
      </div>
      <div class="summary">${escape(summary)}</div>
      <div class="metrics">${i.tokens_input}/${i.tokens_output} tok | $${i.cost_usd}</div>
    `;
    root.appendChild(li);
  }
}

function flashCard(taskId) {
  const card = document.querySelector(`.card[data-task-id="${taskId}"]`);
  if (card) {
    card.classList.remove("flash");
    void card.offsetWidth; // restart animation
    card.classList.add("flash");
  }
}

function escape(s) {
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

// ---------- discovery inbox ----------

function renderDiscovery(items) {
  const root = $("discovery-list");
  $("discovery-count").textContent = items.length;
  root.innerHTML = "";
  if (items.length === 0) {
    root.innerHTML = `<li class="empty">no new items</li>`;
    return;
  }
  for (const item of items) {
    const li = document.createElement("li");
    li.className = "discovery-row";
    li.dataset.itemId = item.id;
    const score = item.signal_score != null ? ` · score ${item.signal_score}` : "";
    li.innerHTML = `
      <div class="title">${escape(item.title)}</div>
      <div class="meta">
        <span class="src">${escape(item.source)}/${escape(item.source_ref || "")}</span>
        <span>${escape(item.kind)}</span>
        <span>${score}</span>
      </div>
      <div class="actions">
        <button class="primary" data-action="promote">Promote</button>
        <button data-action="refine">Refine (PM)</button>
        <button class="danger" data-action="reject">Reject</button>
      </div>
    `;
    li.querySelectorAll("button").forEach((btn) => {
      btn.addEventListener("click", (e) => handleDiscoveryAction(item, btn, e));
    });
    root.appendChild(li);
  }
}

async function handleDiscoveryAction(item, btn, _ev) {
  const action = btn.dataset.action;
  const buttons = btn.parentElement.querySelectorAll("button");
  buttons.forEach((b) => (b.disabled = true));
  try {
    const r = await authedFetch(`/api/discovery/${item.id}/${action}`, { method: "POST" });
    if (action === "refine") {
      const data = await r.json();
      alert(
        `Recommendation: ${data.recommendation}\n` +
        `Module hint: ${data.module_hint ?? "(none)"}\n` +
        `Cost: $${data.cost_usd}\n\n` +
        `Acceptance criteria:\n${data.acceptance_criteria}`
      );
    }
    if (!r.ok) {
      const body = await r.text();
      alert(`${action} failed (${r.status}): ${body}`);
    }
  } catch (e) {
    alert(`${action} error: ${e.message}`);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
    refreshDiscovery();
    scheduleRefresh(100);
  }
}

async function refreshDiscovery() {
  try {
    const r = await authedFetch("/api/discovery");
    if (!r.ok) return;
    renderDiscovery(await r.json());
  } catch (e) {
    console.error("discovery fetch failed", e);
  }
}

// ---------- budget ----------

function renderBudget(snapshot) {
  const root = $("budget-windows");
  const sectionStatus = $("budget-section-status");
  if (!snapshot) {
    root.innerHTML = `<div class="empty">no data</div>`;
    sectionStatus.textContent = "—";
    return;
  }
  const hour = snapshot.hour || {};
  const day = snapshot.day || {};
  const caps = snapshot.caps || {};
  const hourUsed = parseFloat(hour.cost_usd || "0");
  const dayUsed = parseFloat(day.cost_usd || "0");
  const dayCap = parseFloat(caps.day_usd || 0);
  const hourCap = parseFloat(caps.hour_usd || 0);

  // Header badge
  const badge = $("budget-badge");
  let badgeClass = "budget-ok";
  if (snapshot.blocked) badgeClass = "budget-bad";
  else if (dayCap > 0 && dayUsed / dayCap > 0.8) badgeClass = "budget-warn";
  badge.className = badgeClass;
  badge.textContent =
    dayCap > 0
      ? `$${dayUsed.toFixed(4)} / $${dayCap.toFixed(2)}/day`
      : `$${dayUsed.toFixed(4)} (no cap)`;

  // Side panel detail
  let html = "";
  if (snapshot.blocked) {
    html += `<div class="blocked-banner">BUDGET BLOCKED: ${escape(snapshot.reason || "")}</div>`;
  }
  for (const [k, label] of [["hour", "Hour"], ["day", "Day"], ["month", "Month"]]) {
    const w = snapshot[k] || {};
    const cap = caps[`${k}_usd`];
    html += `
      <div class="budget-window">
        <span class="label">${label}</span>
        <span class="value">$${parseFloat(w.cost_usd || "0").toFixed(4)}</span>
        <span class="cap">${cap ? `/ $${cap}` : "no cap"}</span>
      </div>
    `;
  }
  root.innerHTML = html;
  sectionStatus.textContent = snapshot.blocked ? "blocked" : `$${dayUsed.toFixed(2)}`;
}

async function refreshBudget() {
  try {
    const r = await authedFetch("/api/budget");
    if (!r.ok) return;
    renderBudget(await r.json());
  } catch (e) {
    console.error("budget fetch failed", e);
  }
}

// ---------- data ----------

async function refresh() {
  try {
    const r = await authedFetch("/api/kanban");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const board = await r.json();
    state.board = board;
    renderBoard(board);
    renderInstances(board.active_instances || []);
  } catch (e) {
    console.error("refresh failed", e);
  }
  refreshDiscovery();
  refreshBudget();
}

let refreshScheduled = null;
function scheduleRefresh(delay = 250) {
  if (refreshScheduled) return;
  refreshScheduled = setTimeout(() => {
    refreshScheduled = null;
    refresh();
  }, delay);
}

// ---------- live events ----------

function setConnState(on, label) {
  $("conn-state").className = on ? "on" : "off";
  $("conn-label").textContent = label;
}

function setLastEvent(text) { $("last-event").textContent = text; }

function connectWS() {
  // Browsers don't support custom headers on WebSocket — pass the token as a
  // query parameter when auth is configured. Server's check_websocket_token
  // accepts either.
  let url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
  if (TOKEN) url += `?token=${encodeURIComponent(TOKEN)}`;
  const ws = new WebSocket(url);

  ws.onopen = () => setConnState(true, "live");
  ws.onclose = () => {
    setConnState(false, "reconnecting...");
    setTimeout(connectWS, 1500);
  };
  ws.onerror = () => ws.close();

  ws.onmessage = (msg) => {
    let event;
    try { event = JSON.parse(msg.data); } catch { return; }
    setLastEvent(`${event.type} - ${new Date().toLocaleTimeString()}`);

    const p = event.payload || {};
    switch (event.type) {
      case "instance_event":
        if (p.instance_id && p.summary) {
          state.instanceSummary.set(p.instance_id, p.summary);
          if (state.board) renderInstances(state.board.active_instances || []);
        }
        // Usage events shift the budget — refresh that panel.
        if (p.event_type === "usage" || p.event_type === "result") refreshBudget();
        break;
      case "task_transitioned":
        scheduleRefresh(100);
        if (p.task_id) flashCard(p.task_id);
        break;
      case "instance_finalized":
        scheduleRefresh(100);
        refreshBudget();
        break;
      case "job_enqueued":
      case "hook_received":
      case "reviewer_request_changes":
      case "reviewer_needs_human":
        scheduleRefresh(100);
        break;
      case "discovered_item_added":
      case "discovered_item_promoted":
      case "discovered_item_rejected":
      case "discovered_item_refined":
        refreshDiscovery();
        scheduleRefresh(150);
        break;
    }
  };
}

// ---------- bootstrap ----------

(async function init() {
  try {
    const h = await (await fetch("/healthz")).json();
    const badge = $("dry-run-badge");
    badge.textContent = h.dry_run ? "DRY RUN" : "LIVE";
    badge.className = h.dry_run ? "dry" : "live";
  } catch {
    $("dry-run-badge").textContent = "?";
  }

  await refresh();
  setInterval(refresh, 5000); // poll fallback
  connectWS();
})();
