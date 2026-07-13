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

// Roles you can manually dispatch a task to from the UI.
const ROLE_CHOICES = ["dev", "reviewer", "qa", "pm", "po", "design", "devops"];

// Statuses where "Assign" is meaningful. (Discovery/Refinement live in
// other columns; Merged is terminal.)
const ASSIGNABLE_STATUSES = new Set([
  "todo", "in_progress", "blocked", "in_review", "in_qa",
]);

// Natural next step for each status: [targetStatus, buttonLabel]
const ADVANCE_MAP = {
  "todo":        ["in_progress", "▶ Dev"],
  "in_progress": ["in_review",   "▶ Review"],
  "blocked":     ["in_progress", "▶ Unblock"],
  "in_review":   ["in_qa",       "▶ QA"],
  "in_qa":       ["done",        "▶ Done"],
};

function taskCard(t) {
  const needsApproval = Boolean(t.awaiting_approval);
  let cls = "card";
  if (t.last_instance_failed) cls += " card-failed";
  if (needsApproval) cls += " card-awaiting-approval";

  const el = document.createElement("div");
  el.className = cls;
  el.dataset.taskId = t.id;
  const ticket = t.ticket_id ? `<span class="ticket">${escape(t.ticket_id)}</span>` : "";
  const module = t.module_hint ? `<span class="module">@${escape(t.module_hint)}</span>` : "";
  const role = t.current_role_name
    ? `<div class="role-badge">${escape(t.current_role_name)}</div>` : "";

  const approvalBadge = needsApproval
    ? `<div class="approval-badge">⚠ Aguardando aprovação para retornar ao dev</div>` : "";

  const idle = t.current_role_name == null;
  const canAssign = ASSIGNABLE_STATUSES.has(t.status) && idle && !needsApproval;
  const assignBlock = canAssign
    ? `
      <div class="assign-row">
        <select class="role-pick" title="Pick role">
          ${ROLE_CHOICES.map((r) => `<option value="${r}">${r}</option>`).join("")}
        </select>
        <button class="assign-btn">Assign ▸</button>
      </div>` : "";

  // When awaiting approval, replace advance button with approve button.
  let advanceBlock = "";
  if (needsApproval && idle) {
    advanceBlock = `<button class="approve-btn">✓ Aprovar → Dev</button>`;
  } else {
    const advance = ADVANCE_MAP[t.status];
    if (advance && idle) {
      advanceBlock = `<button class="advance-btn" data-target="${advance[0]}">${advance[1]}</button>`;
    }
  }

  el.innerHTML = `
    <div class="title">${escape(t.title)}</div>
    <div class="meta">
      ${ticket}
      <span class="priority ${t.priority}">${t.priority}</span>
      ${module}
    </div>
    ${approvalBadge}
    ${role}
    <div class="card-actions">
      ${advanceBlock}
      ${assignBlock}
    </div>
  `;

  // Open detail modal on click anywhere on card except action buttons.
  el.addEventListener("click", (e) => {
    if (e.target.closest("button, select")) return;
    modal.open(t.id, t.current_instance_id || null);
  });

  if (needsApproval && idle) {
    const btn = el.querySelector(".approve-btn");
    btn.addEventListener("click", () => approveReturnToDev(t.id, btn));
  } else if (canAssign) {
    const select = el.querySelector(".role-pick");
    const btn = el.querySelector(".assign-btn");
    btn.addEventListener("click", () => assignTask(t.id, select.value, btn));
  }
  const advBtn = el.querySelector(".advance-btn");
  if (advBtn) {
    advBtn.addEventListener("click", () => advanceTask(t.id, advBtn.dataset.target, advBtn));
  }
  return el;
}

async function approveReturnToDev(taskId, btn) {
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "enviando...";
  try {
    const r = await authedFetch(`/api/tasks/${taskId}/approve-return-dev`, { method: "POST" });
    if (!r.ok) {
      const body = await r.text();
      alert(`aprovação falhou (${r.status}): ${body}`);
      btn.disabled = false;
      btn.textContent = prev;
    } else {
      btn.textContent = "enviado ao dev ✓";
    }
  } catch (e) {
    alert(`erro: ${e.message}`);
    btn.disabled = false;
    btn.textContent = prev;
  } finally {
    scheduleRefresh(150);
  }
}

async function advanceTask(taskId, targetStatus, btn) {
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "moving...";
  try {
    const r = await authedFetch(`/api/tasks/${taskId}/status`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: targetStatus }),
    });
    if (!r.ok) {
      const body = await r.text();
      alert(`advance failed (${r.status}): ${body}`);
      btn.disabled = false;
      btn.textContent = prev;
    } else {
      const data = await r.json();
      btn.textContent = data.role_triggered ? `queued ${data.role_triggered} ✓` : "moved ✓";
    }
  } catch (e) {
    alert(`advance error: ${e.message}`);
    btn.disabled = false;
    btn.textContent = prev;
  } finally {
    scheduleRefresh(150);
  }
}

async function assignTask(taskId, roleName, btn) {
  btn.disabled = true;
  const prev = btn.textContent;
  btn.textContent = "queuing...";
  try {
    const r = await authedFetch(`/api/tasks/${taskId}/assign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role_name: roleName, priority: 50 }),
    });
    if (!r.ok) {
      const body = await r.text();
      alert(`assign failed (${r.status}): ${body}`);
    } else {
      btn.textContent = "queued ✓";
      setTimeout(() => { btn.textContent = prev; btn.disabled = false; }, 1500);
    }
  } catch (e) {
    alert(`assign error: ${e.message}`);
    btn.disabled = false; btn.textContent = prev;
  } finally {
    scheduleRefresh(150);
  }
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

// ---------- task detail modal ----------

const modal = {
  backdrop: null,
  activeInstanceId: null,  // instance being watched for live events
  liveLogEl: null,         // <ol> element for live events
  init() {
    this.backdrop = $("task-modal");
    $("modal-close").addEventListener("click", () => this.close());
    this.backdrop.addEventListener("click", (e) => {
      if (e.target === this.backdrop) this.close();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") this.close();
    });
  },
  open(taskId, currentInstanceId) {
    this.activeInstanceId = currentInstanceId || null;
    this.liveLogEl = null;
    this.backdrop.classList.remove("hidden");
    $("modal-title").textContent = "Loading…";
    $("modal-description").textContent = "";
    $("modal-body").innerHTML = `<div class="modal-loading">fetching details…</div>`;
    authedFetch(`/api/tasks/${taskId}/detail`)
      .then((r) => r.ok ? r.json() : Promise.reject(r.status))
      .then((d) => this.render(d))
      .catch((e) => { $("modal-body").innerHTML = `<div class="modal-error">Failed to load: ${e}</div>`; });
  },
  close() {
    this.backdrop.classList.add("hidden");
    this.activeInstanceId = null;
    this.liveLogEl = null;
  },
  pushLiveEvent(p) {
    // Called by WS handler when instance_event matches activeInstanceId.
    if (!this.liveLogEl) return;
    const skip = ["system", "rate_limit_event", "heartbeat", "usage", "thinking"];
    if (skip.includes(p.event_type)) return;
    const text = (p.text || "").trim();
    if (!text) return;

    // Merge consecutive MESSAGE (streaming delta) chunks into one block.
    if (p.event_type === "message") {
      const last = this.liveLogEl.lastElementChild;
      if (last && last.dataset.evType === "message") {
        const pre = last.querySelector(".ev-body");
        if (pre) { pre.textContent += text; }
        this.liveLogEl.parentElement.scrollTop = this.liveLogEl.parentElement.scrollHeight;
        return;
      }
    }

    const ts = new Date().toLocaleTimeString();
    const li = document.createElement("li");
    li.className = `ev-item ev-${p.event_type}`;
    li.dataset.evType = p.event_type;
    li.innerHTML = `
      <span class="ev-type">${escape(p.event_type)}</span>
      <span class="ev-time">${ts}</span>
      <pre class="ev-body">${escape(text)}</pre>`;
    this.liveLogEl.appendChild(li);
    this.liveLogEl.parentElement.scrollTop = this.liveLogEl.parentElement.scrollHeight;
  },
  render(d) {
    $("modal-title").textContent = d.title;
    $("modal-description").textContent = d.description || "";
    $("modal-ticket").textContent = d.ticket_id || "";
    $("modal-ticket").style.display = d.ticket_id ? "" : "none";
    const statusEl = $("modal-status");
    statusEl.textContent = d.status;
    statusEl.className = `modal-status-badge status-${d.status}`;

    const hasFailed = d.instances.some((i) => i.status === "failed");
    let html = "";

    // Transition timeline
    if (d.transitions.length) {
      html += `<section class="modal-section"><h3>Timeline</h3><ol class="timeline">`;
      for (const t of d.transitions) {
        const ts = new Date(t.transitioned_at).toLocaleTimeString();
        const reason = t.reason ? `<span class="tl-reason">${escape(t.reason)}</span>` : "";
        html += `<li class="tl-item">
          <span class="tl-time">${ts}</span>
          <span class="tl-arrow">${escape(t.from_status || "—")} → ${escape(t.to_status)}</span>
          ${reason}
        </li>`;
      }
      html += `</ol></section>`;
    }

    // Instances
    if (d.instances.length) {
      html += `<section class="modal-section"><h3>Instances</h3>`;
      for (const inst of d.instances) {
        const failed = inst.status === "failed";
        html += `<details class="inst-block ${failed ? "inst-failed" : ""}" ${failed ? "open" : ""}>
          <summary class="inst-summary">
            <span class="role-badge">${escape(inst.role_name)}</span>
            <span class="inst-status ${inst.status}">${inst.status}</span>
            <span class="inst-meta">${inst.tokens_input}/${inst.tokens_output} tok · $${inst.cost_usd}</span>
            <span class="inst-meta">${new Date(inst.spawned_at).toLocaleTimeString()}${inst.finished_at ? " → " + new Date(inst.finished_at).toLocaleTimeString() : ""}</span>
            ${failed && inst.exit_code != null ? `<span class="inst-exit">exit ${inst.exit_code}</span>` : ""}
          </summary>`;
        if (inst.events.length) {
          html += `<ol class="event-log">`;
          for (const ev of inst.events) {
            const ts = new Date(ev.recorded_at).toLocaleTimeString();
            html += `<li class="ev-item ev-${ev.event_type}">
              <span class="ev-type">${ev.event_type}</span>
              <span class="ev-time">${ts}</span>
              <pre class="ev-body">${escape(ev.summary)}</pre>
            </li>`;
          }
          html += `</ol>`;
        } else {
          html += `<p class="modal-empty">no events recorded</p>`;
        }
        html += `</details>`;
      }
      html += `</section>`;
    }

    if (!html) html = `<p class="modal-empty">No history yet.</p>`;
    $("modal-body").innerHTML = html;

    // If there's a live instance, add a live event stream section.
    if (this.activeInstanceId) {
      const section = document.createElement("section");
      section.className = "modal-section";
      section.innerHTML = `
        <h3><span class="live-dot">●</span> Live stream</h3>
        <ol class="event-log live-log"></ol>`;
      $("modal-body").appendChild(section);
      this.liveLogEl = section.querySelector(".live-log");
    }
  },
};

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
        if (p.instance_id === modal.activeInstanceId) modal.pushLiveEvent(p);
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
      case "role_failure_routed":
      case "task_blocked":
        scheduleRefresh(100);
        break;
      case "reviewer_return_approval_needed":
        scheduleRefresh(100);
        showApprovalToast(p);
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

// ---------- approval toast ----------

function showApprovalToast(p) {
  const id = `toast-${p.task_id}`;
  if (document.getElementById(id)) return; // already showing

  const toast = document.createElement("div");
  toast.id = id;
  toast.className = "approval-toast";
  toast.innerHTML = `
    <div class="toast-header">
      <strong>⚠ Aprovação necessária</strong>
      <button class="toast-close" aria-label="Fechar">✕</button>
    </div>
    <p class="toast-body">
      Revisor solicitou mudanças pela <strong>${p.cycle}ª vez</strong>
      na tarefa <em>${escape(p.task_title || p.task_id)}</em>.<br>
      Aprovação humana necessária para retornar ao Dev.
    </p>
    <div class="toast-actions">
      <button class="toast-approve-btn" data-task-id="${p.task_id}">✓ Aprovar → Dev</button>
      <button class="toast-dismiss-btn">Manter bloqueado</button>
    </div>
  `;

  document.body.appendChild(toast);

  toast.querySelector(".toast-close").addEventListener("click", () => toast.remove());
  toast.querySelector(".toast-dismiss-btn").addEventListener("click", () => toast.remove());
  toast.querySelector(".toast-approve-btn").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "enviando...";
    try {
      const r = await authedFetch(`/api/tasks/${p.task_id}/approve-return-dev`, { method: "POST" });
      if (r.ok) {
        toast.remove();
        scheduleRefresh(150);
      } else {
        btn.textContent = "✗ erro";
        setTimeout(() => { btn.disabled = false; btn.textContent = "✓ Aprovar → Dev"; }, 2000);
      }
    } catch {
      btn.textContent = "✗ erro";
      setTimeout(() => { btn.disabled = false; btn.textContent = "✓ Aprovar → Dev"; }, 2000);
    }
  });

  // Auto-dismiss after 60s
  setTimeout(() => { if (document.getElementById(id)) toast.remove(); }, 60_000);
}

// ---------- settings ----------

const settings = {
  async load() {
    const body = $("settings-body");
    try {
      const list = await (await authedFetch("/api/settings")).json();
      this._render(list);
    } catch {
      body.innerHTML = '<p class="settings-loading">Failed to load settings.</p>';
    }
  },

  _render(list) {
    const body = $("settings-body");
    const groups = {};
    for (const s of list) {
      if (!groups[s.group]) groups[s.group] = [];
      groups[s.group].push(s);
    }
    let html = "";
    for (const [group, items] of Object.entries(groups)) {
      html += `<div class="setting-group"><h4 class="setting-group-title">${group}</h4>`;
      for (const s of items) {
        const inputId = `setting-${s.key}`;
        const sourceClass = `badge-${s.source}`;
        let inputEl;
        if (s.type === "bool") {
          const checked = s.value === "true" || s.value === "True" ? "checked" : "";
          inputEl = `<input type="checkbox" id="${inputId}" data-key="${s.key}" data-type="bool" ${checked}>`;
        } else if (s.sensitive) {
          inputEl = `<input type="password" id="${inputId}" data-key="${s.key}" data-type="${s.type}" value="${s.value === "***" ? "" : s.value}" placeholder="${s.value === "***" ? "••••••" : ""}">`;
        } else {
          inputEl = `<input type="text" id="${inputId}" data-key="${s.key}" data-type="${s.type}" value="${s.value}">`;
        }
        html += `<div class="setting-row">
          <label class="setting-label" for="${inputId}" title="${s.description}">
            ${s.key}
            <span class="setting-source ${sourceClass}">${s.source}</span>
          </label>
          <div class="setting-control">
            ${inputEl}
            <button class="setting-save-btn" data-key="${s.key}" title="Save">✓</button>
            <button class="setting-reset-btn" data-key="${s.key}" title="Reset to default">↺</button>
          </div>
          <p class="setting-desc">${s.description}</p>
        </div>`;
      }
      html += "</div>";
    }
    body.innerHTML = html;

    body.addEventListener("click", async (e) => {
      const saveBtn = e.target.closest(".setting-save-btn");
      const resetBtn = e.target.closest(".setting-reset-btn");
      if (saveBtn) {
        const key = saveBtn.dataset.key;
        const input = body.querySelector(`[data-key="${key}"]`);
        const value = input.type === "checkbox" ? String(input.checked) : input.value;
        await this._save(key, value, saveBtn);
      } else if (resetBtn) {
        await this._reset(resetBtn.dataset.key, resetBtn);
      }
    });
  },

  async _save(key, value, btn) {
    const orig = btn.textContent;
    btn.disabled = true;
    try {
      const res = await authedFetch(`/api/settings/${key}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ value }),
      });
      if (!res.ok) throw new Error(await res.text());
      btn.textContent = "✓ saved";
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; this.load(); }, 1200);
    } catch {
      btn.textContent = "✗ err";
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1500);
    }
  },

  async _reset(key, btn) {
    const orig = btn.textContent;
    btn.disabled = true;
    try {
      await authedFetch(`/api/settings/${key}`, { method: "DELETE" });
      btn.textContent = "↺ done";
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; this.load(); }, 1200);
    } catch {
      btn.textContent = "✗ err";
      setTimeout(() => { btn.textContent = orig; btn.disabled = false; }, 1500);
    }
  },
};

// ---------- roles ----------

const KNOWN_MODELS = [
  "claude-sonnet-4-6",
  "claude-opus-4-8",
  "claude-haiku-4-5-20251001",
  "claude-fable-5",
];

const ALL_TOOLS = ["Read", "Edit", "Write", "Bash", "Grep", "Glob", "WebSearch", "WebFetch"];

const roles = {
  _data: [],

  async load() {
    const body = $("roles-body");
    try {
      this._data = await (await authedFetch("/api/roles")).json();
      this._render();
    } catch {
      body.innerHTML = '<p class="settings-loading">Failed to load roles.</p>';
    }
  },

  _render() {
    const body = $("roles-body");
    if (!this._data.length) {
      body.innerHTML = '<p class="settings-loading">No roles configured.</p>';
      return;
    }
    body.innerHTML = this._data.map((r) => this._roleHtml(r)).join("");

    body.addEventListener("click", async (e) => {
      const saveBtn = e.target.closest(".role-save-btn");
      const resetBtn = e.target.closest(".role-cancel-btn");
      const editBtn = e.target.closest(".role-edit-btn");
      if (editBtn) this._toggleEdit(editBtn.dataset.role, true);
      if (resetBtn) this._toggleEdit(resetBtn.dataset.role, false);
      if (saveBtn) await this._save(saveBtn.dataset.role);
    });
  },

  _roleHtml(r) {
    const modelOpts = KNOWN_MODELS.map((m) =>
      `<option value="${m}" ${m === r.model ? "selected" : ""}>${m}</option>`
    ).join("");
    const toolsChecks = ALL_TOOLS.map((t) => {
      const checked = (r.tools_allowed || []).includes(t) ? "checked" : "";
      return `<label class="tool-check"><input type="checkbox" value="${t}" ${checked}> ${t}</label>`;
    }).join("");

    return `
<div class="role-card" data-role-name="${r.name}" id="role-card-${r.name}">
  <div class="role-card-header">
    <span class="role-card-name">${r.name}</span>
    <div class="role-card-meta">
      <span class="role-meta-badge">${r.model.split("-").slice(1, 3).join("-")}</span>
      <span class="role-meta-badge">${r.max_turns != null ? r.max_turns + " turns" : "∞ turns"}</span>
      <span class="role-meta-badge">${r.max_minutes}min</span>
    </div>
    <button class="role-edit-btn" data-role="${r.name}">✎ Edit</button>
  </div>

  <div class="role-view-mode">
    <p class="role-prompt-preview">${escape(r.system_prompt).substring(0, 120)}${r.system_prompt.length > 120 ? "…" : ""}</p>
  </div>

  <div class="role-edit-mode hidden">
    <div class="role-field">
      <label>Model</label>
      <select class="role-input role-model">
        ${modelOpts}
        <option value="${r.model}" ${!KNOWN_MODELS.includes(r.model) ? "selected" : ""}>${r.model}</option>
      </select>
    </div>
    <div class="role-field role-field-row">
      <div>
        <label>Max turns <span class="role-hint">(null=∞)</span></label>
        <input type="number" class="role-input role-max-turns" min="1" max="100" value="${r.max_turns ?? ""}">
      </div>
      <div>
        <label>Max minutes</label>
        <input type="number" class="role-input role-max-minutes" min="1" max="480" value="${r.max_minutes}">
      </div>
    </div>
    <div class="role-field">
      <label>Tools allowed</label>
      <div class="tools-grid">${toolsChecks}</div>
    </div>
    <div class="role-field">
      <label>System prompt</label>
      <textarea class="role-input role-system-prompt" rows="6">${escape(r.system_prompt)}</textarea>
    </div>
    <div class="role-field role-field-row">
      <div>
        <label>Next role</label>
        <input type="text" class="role-input role-next-role" value="${r.next_role_name ?? ""}">
      </div>
      <div>
        <label>Status on success</label>
        <input type="text" class="role-input role-status-success" value="${r.task_status_on_success ?? ""}">
      </div>
    </div>
    <div class="role-field role-field-row">
      <div>
        <label>Status on failure <span class="role-hint">(vazio=blocked)</span></label>
        <input type="text" class="role-input role-status-failure" value="${r.task_status_on_failure ?? ""}">
      </div>
      <div>
        <label>Max bounces <span class="role-hint">(vazio=∞)</span></label>
        <input type="number" class="role-input role-max-bounces" min="0" max="20" value="${r.max_bounce_count ?? ""}">
      </div>
    </div>
    <div class="role-actions">
      <button class="role-save-btn" data-role="${r.name}">✓ Salvar</button>
      <button class="role-cancel-btn" data-role="${r.name}">Cancelar</button>
    </div>
  </div>
</div>`;
  },

  _toggleEdit(roleName, open) {
    const card = document.getElementById(`role-card-${roleName}`);
    if (!card) return;
    card.querySelector(".role-view-mode").classList.toggle("hidden", open);
    card.querySelector(".role-edit-mode").classList.toggle("hidden", !open);
  },

  async _save(roleName) {
    const card = document.getElementById(`role-card-${roleName}`);
    const saveBtn = card.querySelector(".role-save-btn");
    saveBtn.disabled = true;
    saveBtn.textContent = "Salvando…";

    const tools = [...card.querySelectorAll(".tool-check input:checked")].map((el) => el.value);
    const maxTurnsRaw = card.querySelector(".role-max-turns").value.trim();

    const body = {
      model: card.querySelector(".role-model").value,
      max_turns: maxTurnsRaw === "" ? null : parseInt(maxTurnsRaw, 10),
      max_minutes: parseInt(card.querySelector(".role-max-minutes").value, 10),
      tools_allowed: tools,
      system_prompt: card.querySelector(".role-system-prompt").value,
      next_role_name: card.querySelector(".role-next-role").value.trim() || null,
      task_status_on_success: card.querySelector(".role-status-success").value.trim() || null,
      task_status_on_failure: card.querySelector(".role-status-failure").value.trim() || null,
      max_bounce_count: (() => {
        const raw = card.querySelector(".role-max-bounces").value.trim();
        return raw === "" ? null : parseInt(raw, 10);
      })(),
    };

    try {
      const res = await authedFetch(`/api/roles/${roleName}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(await res.text());
      saveBtn.textContent = "✓ Salvo";
      await this.load(); // re-render with new values
      setTimeout(() => this._toggleEdit(roleName, false), 400);
    } catch (e) {
      saveBtn.textContent = "✗ Erro";
      saveBtn.title = e.message;
      setTimeout(() => { saveBtn.disabled = false; saveBtn.textContent = "✓ Salvar"; }, 2000);
    }
  },
};

// ---------- bootstrap ----------

(async function init() {
  modal.init();
  settings.load();
  roles.load();

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
