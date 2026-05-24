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

// ---------- data ----------

async function refresh() {
  try {
    const r = await fetch("/api/kanban");
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const board = await r.json();
    state.board = board;
    renderBoard(board);
    renderInstances(board.active_instances || []);
  } catch (e) {
    console.error("refresh failed", e);
  }
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
  const url = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws";
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
          // re-render instances cheaply
          if (state.board) renderInstances(state.board.active_instances || []);
        }
        break;
      case "task_transitioned":
        scheduleRefresh(100);
        if (p.task_id) flashCard(p.task_id);
        break;
      case "instance_finalized":
      case "job_enqueued":
      case "hook_received":
        scheduleRefresh(100);
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
