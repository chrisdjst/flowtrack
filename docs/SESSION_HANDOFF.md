# Session handoff

Last updated at the end of the session that delivered PR #1 (orchestrator
foundation) and PR #2 (CLI bridge). Use this to pick up in a clean session
without re-reading the chat history.

For the *design* (why this exists, architecture, roadmap), read
[`ORCHESTRATOR.md`](./ORCHESTRATOR.md). This file is just operational state.

---

## Where main is

```
d571d2e Merge pull request #2 from chrisdjst/feat/cli-orchestrator-bridge
564e692 Add CLI commands that bridge to the orchestrator
e530bbc Add Assign button + role select to kanban cards
a7cd539 Merge pull request #1 from chrisdjst/feat/orchestrator-foundation
35c44e9 Close the autonomous loop: auto-refine + reviewer verdicts + frontend + auth
2575f13 Add Sentry discovery source
4c57375 Add Sentry error monitoring for the FlowTrack daemon
911b2d4 Add GitHub discovery + PM agent + watchdog retry + smoke fixes
730e2ff Fix spawner for real Claude Code CLI + extend stream-json parser
d2d1102 Add branch chaining, budget breaker, orphan-claim recovery, Jira discovery
ce5340a Add worker_id isolation lanes for jobs + instances
ca2ec4c Add minimal kanban frontend served from FastAPI
c5c53c4 Add WebSocket event broker + worktree hook callback
f476ac6 Add role pipeline (dev->reviewer->qa) and watchdog
fee9646 Add orchestrator: spawn pipeline + dry-run loop + smoke harness
f45284f Add FastAPI daemon with kanban/tasks/instances endpoints
4bfebab Add orchestrator DB schema (migration 005 + 8 new models)
b26c46a Add orchestrator design doc
```

Branches `feat/orchestrator-foundation` and `feat/cli-orchestrator-bridge`
are both merged and deleted.

---

## What's in place

| Subsystem | Status |
|---|---|
| Alembic schema (001–008) | `008 (head)` |
| Orchestrator (loop, queue, locks, watchdog, budget, worker_id) | done |
| Spawn pipeline (worktree + branch chaining + hooks + stream parsing) | done; **validated real** vs `claude` 2.1.150 |
| Pipeline roles (dev → reviewer → qa, data-driven via `roles` columns) | done |
| Reviewer semantic verdicts (APPROVE / REQUEST_CHANGES / NEEDS_HUMAN) | done |
| WebSocket `/ws` + EventBroker fan-out | done |
| Frontend (vanilla HTML/CSS/JS, kanban + discovery inbox + budget + Assign button) | done |
| Discovery sources: Jira, GitHub Issues, Sentry | done; **Sentry validated real** vs `origin-nebula/python` |
| PM refinement agent (Claude OAuth, JSON output, ~$0.04/call) | done; **validated real** |
| Auto-refine on new discovered items (off by default) | done |
| Sentry SDK error monitoring for the daemon | done (off when DSN empty) |
| Bearer-token auth on `/api/*` + `/ws` (off when token empty) | done |
| CLI bridge: `task update --criteria/--module-hint`, `task assign`, enriched `task list`/`show`, `discovery` subapp | done |

---

## Quick start (after a session clear)

```sh
cd C:/workspace/flowtrack

# 1. Postgres
docker compose up -d db

# 2. Confirm schema is at head
FLOWTRACK_DATABASE_URL="postgresql://flowtrack:flowtrack@localhost:5433/flowtrack" \
  uv run alembic current   # expect "008 (head)"

# 3. Run smokes (~5 min total, no real Claude spend)
for s in worker_isolation orchestrator watchdog pipeline websocket budget \
         discovery github_discovery sentry sentry_discovery auto_refine \
         reviewer_verdict api_auth cli; do
  uv run python scripts/smoke_$s.py >/dev/null 2>&1 && echo "$s: PASS" || echo "$s: FAIL"
done

# 4. Boot the daemon for local play (free, mock-claude does the work)
FLOWTRACK_DATABASE_URL="postgresql://flowtrack:flowtrack@localhost:5433/flowtrack" \
FLOWTRACK_ORCHESTRATOR_DRY_RUN=false \
FLOWTRACK_MAX_CONCURRENT_INSTANCES=1 \
FLOWTRACK_ORCHESTRATOR_LOOP_INTERVAL_SECONDS=0.5 \
FLOWTRACK_CLAUDE_EXECUTABLE="python C:/workspace/flowtrack/scripts/mock_claude.py" \
FLOWTRACK_TARGET_REPO_PATH=C:/workspace/flowtrack \
FLOWTRACK_WORKTREE_ROOT=C:/workspace/flowtrack/.demo-worktrees \
  uv run flowtrack-server &

# 5. Drive the pipeline via CLI (kanban at http://127.0.0.1:8080/)
flowtrack task add "Probe X" --priority high --no-jira
flowtrack task update <id-prefix> --criteria "1. ..." --module-hint demo
flowtrack task assign <id-prefix> dev
# wait ~3s, then:
flowtrack task show <id-prefix>
```

Stop the daemon:

```powershell
Get-NetTCPConnection -LocalPort 8080 -State Listen | Select-Object -ExpandProperty OwningProcess -Unique | ForEach-Object { Stop-Process -Id $_ -Force }
```

To run **real Claude** instead of mock: drop the `FLOWTRACK_CLAUDE_EXECUTABLE`
line above. Cost per dev-only spawn ≈ $0.14 (Sonnet); full chain ≈ $0.40–0.60.
Set `FLOWTRACK_BUDGET_DAY_CAP_USD=1.00` for safety.

---

## Credentials & secrets

All real creds live in `C:/workspace/flowtrack/.env` (gitignored). Defaults
in `.env.example` are documented.

| Var | Purpose | Notes |
|---|---|---|
| `FLOWTRACK_DATABASE_URL` | Postgres connection | localhost:5433 via docker |
| `FLOWTRACK_ANTHROPIC_API_KEY` | Subprocess auth (overrides Claude Code OAuth) | leave empty to use Claude Code login |
| `FLOWTRACK_API_TOKEN` | Bearer token for `/api/*` + `/ws` | empty = no auth (localhost-only assumed) |
| `FLOWTRACK_SENTRY_DSN` | error sink for the daemon (sentry-sdk) | got via DSN, NOT auth token |
| `FLOWTRACK_SENTRY_TOKEN` | discovery source — read issues from Sentry API | **distinct** from DSN; needs scopes `org:read project:read event:read issue:read` |
| `FLOWTRACK_SENTRY_ORG` / `FLOWTRACK_SENTRY_PROJECT` | Sentry org + project slugs | currently `origin-nebula` / `python` |
| `FLOWTRACK_JIRA_*` | Jira discovery + sync | unset by default |
| `FLOWTRACK_GITHUB_*` | GitHub discovery + PR comments | unset by default |

**Action item — Sentry token rotation:** the token
`12f0309baedcca589d21f7152973ddc163f39d659b44b8f284ebaf4cbb68e01d` was
shared in the working chat transcript. It's an Internal Integration token
on `origin-nebula`. Revoke it and issue a new one at
<https://sentry.io/settings/origin-nebula/developer-settings/>. The
discovery source code is ready to consume the replacement with no edits.

---

## Smoke catalogue (14 mock + 2 real)

| Smoke | Cost | What it covers |
|---|---|---|
| `smoke_worker_isolation` | $0 | claim filter by worker_id lane |
| `smoke_orchestrator` | $0 (mock) | single instance lifecycle end-to-end |
| `smoke_watchdog` | $0 | stale heartbeats killed + orphan claims retry-or-fail |
| `smoke_pipeline` | $0 (mock) | dev → reviewer → qa chain + branch chaining verified via `git merge-base --is-ancestor` |
| `smoke_websocket` | $0 (mock) | live event fan-out (35 events across 5 types) |
| `smoke_budget` | $0 (mock) | circuit breaker blocks spawn at cap |
| `smoke_discovery` | $0 | Jira backlog source (mocked client) + promote API |
| `smoke_github_discovery` | $0 | GitHub Issues source (mocked, PRs filtered, kind inference) |
| `smoke_sentry` | $0 | sentry-sdk wiring (fake transport, no network) |
| `smoke_sentry_discovery` | $0 | Sentry source (mocked client, score clamp, min_event_count) |
| `smoke_auto_refine` | $0 | manager auto-promotes/rejects per PM recommendation |
| `smoke_reviewer_verdict` | $0 (mock) | REQUEST_CHANGES sends back to dev, NEEDS_HUMAN blocks |
| `smoke_api_auth` | $0 | bearer-token gate on `/api/*` and `/ws`, 9 checks |
| `smoke_cli` | $0 | Typer CliRunner across 10 new commands/flags |
| `smoke_real_claude` | ~$0.14 | full dev spawn against real `claude` CLI |
| `smoke_pm_agent` | ~$0.08 | PM agent against real Claude, 2 cases |

`scripts/debug_claude_invoke.py` is a probe (not a smoke) for diagnosing
`claude` CLI flag/format mismatches when the version bumps. ~$0.001/run.

---

## Known gotchas (won't re-discover)

1. **`claude --print --output-format stream-json` requires `--verbose`**.
   Without it the process exits 1 silently with `events=0` in DB. Spawner
   passes `--verbose` always.
2. **First `system/init` line from real Claude > 64 KB.** Default
   `asyncio.StreamReader.readline()` raises `LimitOverrunError`. Spawner
   passes `limit=4 * 1024 * 1024`.
3. **Real Claude `usage` shape differs from mock**: per-turn `usage` is
   nested in `assistant.message.usage`; final totals are in `result.usage`
   and `result.total_cost_usd`. `stream_parser._extract_usage` handles both;
   when `result.total_cost_usd` is present it's used as authoritative.
4. **FastAPI yield-dependency commits AFTER response delivery.** Rapid
   client retries see stale state. Mutating endpoints (`/promote`,
   `/reject`) call `db.commit()` explicitly before returning.
5. **`Depends`-style sessions detach ORM objects on context exit.** CLI
   list-style commands build their Rich tables INSIDE the `with get_db()`
   block; printing later is fine because Rich caches.
6. **Two daemons racing on the same job queue.** Each daemon owns a
   `worker_id` (`<hostname>:<pid>:<6hex>` by default); claim query filters
   `worker_id IS NULL OR worker_id == self`. Smokes opt into a private lane
   via `FLOWTRACK_WORKER_ID=smoke-<...>`.
7. **Sentry has Auth Tokens vs DSNs vs Internal Integration Client Secrets**
   — all 64 hex chars, only Auth Token / IIntegration Token authorise
   `/api/0/*` calls. We use the Internal Integration Token from the
   "Tokens" section of the integration's settings page.
8. **Rich table truncates cell values to fit the terminal**, including in
   CliRunner. Smoke assertions match on short tags (e.g. a 6-char run ID),
   not full source_refs.
9. **`Depends(db_session)` runs cleanup AFTER response.** Multi-step
   wizards that re-POST need explicit `db.commit()` before returning.
10. **`asyncio.create_subprocess_exec` + Windows + cmdline with backslashes:**
    `shlex.split(posix=False)` keeps surrounding quotes inside the token —
    strip them before subprocess. Spawner does this for `claude_executable`.

---

## Open work, in priority order

These are the candidates from the end of the session. Pick any, they're
independent.

1. **Reviewer free-text feedback in dev re-spawn.** Today the reviewer's
   verdict (`APPROVE` / `REQUEST_CHANGES` / `NEEDS_HUMAN`) is parsed, but
   the textual feedback isn't piped into the next dev's prompt on a
   `REQUEST_CHANGES`. Plan: extract the last assistant message's text and
   stash it in `payload_json.reviewer_feedback`; `_build_prompt` reads it
   when present.
2. **24/7 hardening.** systemd unit (or `restart: always` in
   docker-compose), retry-with-backoff in `stream_parser` for transient
   Anthropic API outages, Slack webhook on budget breach. None of these
   exist yet.
3. **Per-source auto-refine policy.** Today `auto_refine_discovered` is a
   single boolean. Should be a per-source map (e.g. Sentry: always refine;
   Jira: only if `priority=high`; GitHub: only with label `auto-refine`).
4. **Drag-and-drop kanban + Next.js separate repo.** The vanilla
   `flowtrack/web/static/` is the MVP; per `ORCHESTRATOR.md §8` the real
   frontend should live in its own repo with Next.js + shadcn. Not started.
5. **Sentry token rotation.** See "Credentials" section above.
6. **More CLI ergonomics.** `flowtrack instance list/kill/logs`,
   `flowtrack pipeline status` (current task at each stage). Not started.
7. **`flowtrack discovery refresh <source>`** to manually trigger a source
   pull (e.g. after fixing creds). Not started.

---

## Last known runtime state at the time of writing

- No `flowtrack-server` daemon running (port 8080 free)
- Postgres container `flowtrack-db` up + healthy
- DB has demo seed data: 2 discovery items + 4 tasks (one in `merged`, two
  in `refinement`/`ready`, one fresh `CLI demo: end-to-end` in `merged`).
  Anything you don't want is fine to nuke — see the cleanup SQL in the
  conversation history or rerun the seed snippets below.

If you want to wipe orchestrator data and keep only roles (clean slate):

```sql
TRUNCATE task_transitions, task_comments, instance_events, resource_locks,
         job_queue, instances, discovered_items, budget_windows
  RESTART IDENTITY CASCADE;
DELETE FROM tasks;
```

Re-seed demo discovery + tasks:

```sql
INSERT INTO discovered_items (id, source, source_ref, kind, title, summary, signal_score, status, created_at)
VALUES
  (gen_random_uuid(), 'sentry', 'DEMO-1', 'bug', 'Login form throws 500 on empty password',
   'Repro: POST /api/auth/login with username only crashes AuthController:42', 12.0, 'new', NOW()),
  (gen_random_uuid(), 'github_issue', '#42', 'feature', 'Add CSV export to /report',
   'Multiple users asking for CSV alongside the existing table/JSON outputs.', NULL, 'new', NOW());

INSERT INTO tasks (id, title, description, status, priority, ticket_id, module_hint, acceptance_criteria, created_at)
VALUES
  (gen_random_uuid(), 'Demo: ready task', 'You can assign this to dev role via the kanban or CLI.',
   'todo', 'high', 'DEMO-READY', 'demo',
   '1. File demo.txt exists. 2. Commit message starts with DEMO-READY.', NOW()),
  (gen_random_uuid(), 'Demo: unrefined idea', 'No acceptance criteria yet — needs PM refinement.',
   'todo', 'medium', 'DEMO-IDEA', NULL, NULL, NOW()),
  (gen_random_uuid(), 'Demo: code under review', 'Pretend dev finished and reviewer is looking.',
   'in_review', 'medium', 'DEMO-REVIEW', 'demo', '1. Code is reviewed.', NOW());
```
