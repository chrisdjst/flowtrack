# FlowTrack Orchestrator — Mapeamento Completo

> Documento de design para transformar FlowTrack (CLI de métricas) em um orquestrador
> multi-instância de Claude Code com kanban, descoberta automática de tarefas e
> simulação de papéis de TI (PM, PO, Dev, QA, etc.) rodando 24/7.

**Status:** proposta. Nada aqui foi implementado ainda.
**Audiência:** quem vai implementar (você, e/ou agents trabalhando neste repo).
**Princípio:** todas as decisões marcadas como _"Recomendado"_ podem ser substituídas
sem reescrever o resto do doc — elas são pluggable. Decisões marcadas como
_"Fundacional"_ amarram o resto do design e mudá-las exige revisar tudo.

---

## 0. TL;DR

A extensão é viável e a maior parte do trabalho é **infraestrutura nova**, não reescrita.
O FlowTrack atual continua funcionando como CLI local (modo "single-developer") em
paralelo ao modo "orchestrator" — eles compartilham o mesmo banco e os mesmos modelos
de domínio (`Session`, `Task`, `Event`, etc.), com tabelas novas para
instâncias/papéis/locks/queue.

**Esforço estimado** (1 dev sênior, sem ajuda):

| Fase | Escopo | Semanas |
|------|--------|---------|
| 1 | API daemon + 1 worker spawnando Claude headless em 1 task de Jira | 2–3 |
| 2 | Kanban frontend lendo do banco via WebSocket | 1–2 |
| 3 | Múltiplas instâncias com locking por módulo + circuit breaker | 2 |
| 4 | Pipeline Dev → Review (agent reviewer) → merge manual | 2 |
| 5 | QA agent + integração Sentry pra descoberta de bugs | 3 |
| 6 | PM/PO/Design agents (refinamento de backlog) | 3–4 |
| 7 | 24/7 hardening: observabilidade, retry, budget enforcement | 3 |
| **Total MVP utilizável** | até fase 4 | **7–9 semanas** |
| **Total visão completa** | até fase 7 | **16–20 semanas** |

**O que vai te quebrar** (em ordem de probabilidade): conflitos entre instâncias
mexendo no mesmo código, agents que aprovam reviews ruins, custo escalando sem teto,
descoberta de tarefas alucinando escopo. Cada um tem mitigação prevista abaixo, mas
nenhuma é gratuita.

---

## 1. Estado atual (resumo do que existe hoje)

FlowTrack hoje é um **CLI stateless** com banco Postgres. Cada invocação
(`flowtrack dev start`, `flowtrack task add`, etc.) abre conexão, faz o trabalho,
sai. Não há daemon, não há API, não há fila.

**Tabelas existentes** (não mexer no schema delas, só adicionar colunas):

| Tabela | Papel |
|--------|-------|
| `sessions` | Sessão de trabalho (dev/review/test) com início/fim, ticket, PR |
| `events` | Eventos dentro de sessão: block/interrupt/pause/resume + metadata JSONB |
| `tasks` | Tarefas: title, status (todo/in_progress/blocked/in_review/done), priority, ticket |
| `task_comments` | Comentários em tasks, com flag `synced_to_jira` |
| `deployments` | Deploys: env, commit_sha, PR, ticket |
| `incidents` | Incidentes ligados a deploy: severity, resolved_at |
| `config` | Key-value com criptografia AES opcional (tokens GitHub/Jira) |

**Integrações existentes:** GitHub (comentar em PR), Jira (criar issue, comentar).

**O que não existe e o orquestrador vai precisar:** daemon HTTP, fila de jobs,
locks distribuídos, registro de instâncias de Claude Code, métricas de custo (tokens),
descoberta de tarefas, frontend.

---

## 2. Arquitetura alvo

```
┌─────────────────────────────────────────────────────────────────────┐
│                     FRONTEND (Next.js)                              │
│  Kanban: colunas Discovery → Refine → Dev → Review → QA → Merged    │
│  Cards = tasks. Avatares = instâncias de Claude. Stream via WS.     │
└───────────────▲────────────────────────────────────────▲────────────┘
                │ WebSocket (eventos)                   │ REST (CRUD)
                │                                       │
┌───────────────┴───────────────────────────────────────┴────────────┐
│              FlowTrack API (FastAPI) — único daemon                │
│  /api/tasks  /api/instances  /api/sessions  /api/discovery  /ws    │
└──┬───────────────┬─────────────────┬──────────────────┬────────────┘
   │ async         │                 │                  │
   ▼               ▼                 ▼                  ▼
┌──────────┐ ┌─────────────┐ ┌───────────────┐ ┌─────────────────┐
│Discovery │ │Orchestrator │ │ Cost / Budget │ │ Sync Workers    │
│Workers   │ │   Loop      │ │   Enforcer    │ │ (GitHub, Jira)  │
│(Sentry,  │ │             │ │               │ │ (existem hoje,  │
│ Jira,    │ │ Pega task   │ │ Mata sessão   │ │  passam a rodar │
│ GH issues│ │ + papel +   │ │ que estourou  │ │  no daemon)     │
│ )        │ │ spawn Claude│ │ orçamento     │ │                 │
└──────────┘ └──────┬──────┘ └───────────────┘ └─────────────────┘
                   │ spawn `claude --print --output-format stream-json`
                   ▼
        ┌─────────────────────────────────┐
        │   Pool de processos Claude Code │
        │   (1 processo = 1 instance row) │
        │                                 │
        │   Cada um roda numa worktree    │
        │   git isolada + tem session_id  │
        │   + hooks que reportam progresso│
        │   pro daemon via HTTP local     │
        └─────────────────────────────────┘
                   │
                   ▼
          ┌────────────────────┐
          │  Postgres (mesmo   │
          │  banco FlowTrack)  │
          └────────────────────┘
```

**Decisão fundacional 1:** o daemon é **um processo só** (FastAPI + workers async no
mesmo event loop ou via APScheduler). Não introduz Celery/Redis no MVP. Postgres
serve como fila usando `SELECT ... FOR UPDATE SKIP LOCKED`. Quando o load passar de
~50 tasks/dia, migrar pra Redis/RQ. Adicionar Celery agora é over-engineering.

**Decisão fundacional 2:** **uma instância de Claude Code = um processo headless
nativo** (`claude --print --session-id <uuid> --output-format stream-json`), não
Claude Agent SDK em Python. Razão: as skills do FlowTrack já estão escritas como
slash commands do Claude Code; usar o CLI nativo significa que as skills funcionam
sem porte. Tradeoff: você não tem controle fino do agent loop, mas ganha tudo que o
Claude Code já faz (hooks, sub-agents, tool routing).

**Decisão fundacional 3:** **isolamento por git worktree**, não por container. Cada
instância de Claude Code roda numa worktree separada do repo alvo. Bem mais leve
que Docker e o `EnterWorktree` do próprio Claude Code já implementa isso.

---

## 3. Mudanças no banco

Todas as tabelas novas vão em uma migration nova:
`alembic/versions/005_orchestrator_schema.py`.

### 3.1. Novas tabelas

```sql
-- Catálogo de papéis. Seed inicial: pm, po, design, dev, reviewer, qa, devops.
CREATE TABLE roles (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  name          varchar(50) UNIQUE NOT NULL,     -- 'dev', 'reviewer', 'qa', ...
  system_prompt text        NOT NULL,            -- prompt base do papel
  tools_allowed text[],                          -- whitelist de ferramentas
  model         varchar(50) NOT NULL DEFAULT 'claude-sonnet-4-6',
  max_tokens    int         NOT NULL DEFAULT 500000,  -- budget por sessão
  max_minutes   int         NOT NULL DEFAULT 60,      -- circuit breaker
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

-- Cada linha = um processo Claude Code rodando (ou que rodou).
CREATE TABLE instances (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  role_id       uuid REFERENCES roles(id) NOT NULL,
  task_id       uuid REFERENCES tasks(id),                -- task atual (nullable se idle)
  session_id    uuid REFERENCES sessions(id),             -- ligação com FlowTrack session
  claude_session_id varchar(100),                         -- --session-id do Claude Code
  pid           int,                                      -- PID do processo
  worktree_path text,                                     -- path da worktree git
  branch_name   varchar(255),
  status        varchar(20) NOT NULL,
                  -- 'spawning'|'running'|'waiting_input'|'completed'|'failed'|'killed'
  spawned_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  exit_code     int,
  tokens_input  bigint NOT NULL DEFAULT 0,
  tokens_output bigint NOT NULL DEFAULT 0,
  cost_usd      numeric(10,4) NOT NULL DEFAULT 0,
  last_heartbeat_at timestamptz,                          -- pro watchdog matar zumbi
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX idx_instances_status ON instances(status) WHERE status IN ('spawning','running','waiting_input');
CREATE INDEX idx_instances_task   ON instances(task_id);

-- Fila de jobs. Quando orchestrator pega job: UPDATE SET claimed_at=now()
-- WHERE id=(SELECT id FROM job_queue WHERE claimed_at IS NULL ORDER BY priority,created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING *;
CREATE TABLE job_queue (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       uuid REFERENCES tasks(id) NOT NULL,
  role_id       uuid REFERENCES roles(id) NOT NULL,
  priority      int  NOT NULL DEFAULT 100,        -- menor = mais urgente
  payload_json  jsonb NOT NULL DEFAULT '{}'::jsonb, -- input pro Claude
  status        varchar(20) NOT NULL DEFAULT 'queued',
                  -- 'queued'|'claimed'|'running'|'done'|'failed'|'cancelled'
  attempts      int  NOT NULL DEFAULT 0,
  max_attempts  int  NOT NULL DEFAULT 3,
  claimed_at    timestamptz,
  claimed_by    uuid REFERENCES instances(id),
  finished_at   timestamptz,
  last_error    text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_job_queue_pending ON job_queue(priority, created_at) WHERE status = 'queued';

-- Locks lógicos por "recurso" (módulo, arquivo, package). Duas instâncias não podem
-- pegar o mesmo lock. Renovado por heartbeat, expira sozinho se a instância morrer.
CREATE TABLE resource_locks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  resource_key  varchar(255) NOT NULL,    -- ex.: 'flowtrack/services/sync_service.py'
                                          -- ou 'module:billing' ou 'migration'
  instance_id   uuid REFERENCES instances(id) NOT NULL,
  acquired_at   timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,     -- now() + N minutos, renovado via heartbeat
  UNIQUE (resource_key)
);

-- Transições de coluna do kanban. Audit trail + métrica de cycle time.
CREATE TABLE task_transitions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  task_id       uuid REFERENCES tasks(id) NOT NULL,
  from_status   varchar(50),
  to_status     varchar(50) NOT NULL,
  instance_id   uuid REFERENCES instances(id),
  reason        text,
  transitioned_at timestamptz NOT NULL DEFAULT now()
);

-- Tarefas "candidatas" descobertas automaticamente. Vão pra coluna Discovery do
-- kanban; humano (ou um PM agent) decide promover pra tasks reais.
CREATE TABLE discovered_items (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source        varchar(50) NOT NULL,     -- 'sentry'|'jira'|'github_issue'|'pr_comment'|'log_pattern'
  source_ref    varchar(255),             -- id externo (sentry event id, issue #, etc.)
  kind          varchar(30) NOT NULL,     -- 'bug'|'feature'|'improvement'|'tech_debt'|'incident'
  title         text NOT NULL,
  summary       text,
  raw_payload   jsonb,                    -- payload bruto da fonte
  signal_score  numeric(5,2),             -- heurística da fonte (frequência, severidade)
  status        varchar(20) NOT NULL DEFAULT 'new',
                  -- 'new'|'promoted'|'rejected'|'duplicate'
  promoted_task_id uuid REFERENCES tasks(id),
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, source_ref)
);

-- Eventos de stream-json do Claude Code. Buffer pra debug + frontend mostrar
-- "o que a instância está fazendo agora". Pode ficar gigante — TTL via cron.
CREATE TABLE instance_events (
  id            bigserial PRIMARY KEY,
  instance_id   uuid REFERENCES instances(id) NOT NULL,
  event_type    varchar(50) NOT NULL,    -- 'tool_use'|'message'|'error'|'thinking'|'heartbeat'
  payload_json  jsonb NOT NULL,
  recorded_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_instance_events_by_instance ON instance_events(instance_id, recorded_at DESC);
-- Particionar por dia se ficar grande. TTL: deletar > 30 dias.

-- Orçamento agregado pra circuit breaker global.
CREATE TABLE budget_windows (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  window_start  timestamptz NOT NULL,    -- início da janela (hora cheia, dia, etc.)
  window_kind   varchar(20) NOT NULL,    -- 'hour'|'day'|'month'
  tokens_used   bigint NOT NULL DEFAULT 0,
  cost_usd      numeric(12,4) NOT NULL DEFAULT 0,
  task_count    int NOT NULL DEFAULT 0,
  UNIQUE (window_start, window_kind)
);
```

### 3.2. Alterações em tabelas existentes

**`sessions`** — adicionar:
- `instance_id uuid REFERENCES instances(id)` (nullable; sessão CLI manual continua tendo NULL)

**`tasks`** — adicionar:
- `discovered_from uuid REFERENCES discovered_items(id)` (nullable)
- `module_hint varchar(100)` — heurística pra locking ("billing", "auth", etc.)
- `acceptance_criteria text` — preenchido pelo PM agent na fase 6
- `parent_task_id uuid REFERENCES tasks(id)` — subdivisão por papéis na mesma feature

**`task_comments`** — adicionar:
- `author_role_id uuid REFERENCES roles(id)` (nullable; humano = NULL)
- `instance_id uuid REFERENCES instances(id)` (nullable)

**`config`** — sem mudanças. Já serve pra guardar token Anthropic, configs do orquestrador, etc.

### 3.3. Status do kanban

Hoje `tasks.status` é enum: `todo|in_progress|blocked|in_review|done`. **Manter** mas
expandir as colunas do kanban via view, não no enum (enum dá dor pra evoluir):

```
Discovery       ← discovered_items WHERE status='new'
Refinement      ← tasks WHERE status='todo' AND acceptance_criteria IS NULL
Ready           ← tasks WHERE status='todo' AND acceptance_criteria IS NOT NULL
In Progress     ← tasks WHERE status='in_progress'
Blocked         ← tasks WHERE status='blocked'
In Review       ← tasks WHERE status='in_review'
QA              ← novo: status='in_qa' (adicionar ao enum)
Merged          ← tasks WHERE status='done'
```

Adicionar `'in_qa'` ao enum é a única alteração de enum necessária. Tudo o mais é
derivado de outras colunas via view materializada `kanban_board_v` (refresh a cada
event do orquestrador).

---

## 4. Nova camada de API (FastAPI daemon)

### 4.1. Estrutura de arquivos

```
flowtrack/
├── api/                          ← NOVO
│   ├── __init__.py
│   ├── app.py                    ← FastAPI instance + middlewares
│   ├── deps.py                   ← Depends(get_db), auth, rate limit
│   ├── ws.py                     ← WebSocket manager
│   └── routers/
│       ├── tasks.py              ← CRUD de tasks (envelope sobre task_service)
│       ├── instances.py          ← lista, kill, logs de instâncias
│       ├── jobs.py               ← enqueue, status da fila
│       ├── discovery.py          ← lista discovered_items, promove
│       ├── kanban.py             ← endpoint single pro frontend (board completo)
│       ├── roles.py              ← CRUD de roles
│       ├── budget.py             ← janelas de orçamento, set limits
│       └── webhooks.py           ← receber GitHub/Jira/Sentry
├── orchestrator/                 ← NOVO
│   ├── __init__.py
│   ├── loop.py                   ← async loop principal
│   ├── spawner.py                ← subprocess Claude Code
│   ├── stream_parser.py          ← parse de --output-format stream-json
│   ├── locks.py                  ← acquire/release/renew resource_locks
│   ├── budget.py                 ← enforcement de tokens/tempo/$ por sessão
│   ├── watchdog.py               ← mata zumbis (last_heartbeat_at velho)
│   └── worktree.py               ← git worktree create/remove
├── discovery/                    ← NOVO
│   ├── __init__.py
│   ├── base.py                   ← interface DiscoverySource
│   ├── sources/
│   │   ├── jira_backlog.py       ← lê tasks com label "auto"
│   │   ├── sentry.py             ← API Sentry, agrupa por fingerprint
│   │   ├── github_issues.py
│   │   └── pr_comments.py        ← "please fix" em PRs
│   └── scheduler.py              ← roda cada fonte em interval
└── main.py                       ← adicionar comando `flowtrack serve`
```

### 4.2. Endpoints principais

```
# Tasks / kanban
GET    /api/kanban                       → board inteiro (todas colunas + cards)
GET    /api/tasks                        → list (filtro por status, role, etc.)
POST   /api/tasks                        → cria task
PATCH  /api/tasks/{id}                   → muda status (registra transition)
POST   /api/tasks/{id}/assign            → cria job na fila pro papel X
GET    /api/tasks/{id}/timeline          → transitions + comments + events

# Instances
GET    /api/instances                    → ativas + recentes
GET    /api/instances/{id}/events        → stream de eventos (paginado)
POST   /api/instances/{id}/kill          → SIGTERM (com grace period)
POST   /api/instances/{id}/interrupt     → injeta mensagem (usa /resume)

# Discovery
GET    /api/discovery                    → discovered_items pendentes
POST   /api/discovery/{id}/promote       → vira task real
POST   /api/discovery/{id}/reject        → marca rejected
POST   /api/discovery/refresh            → força refresh de uma fonte

# Roles
GET/POST/PATCH/DELETE /api/roles

# Budget
GET    /api/budget                       → janelas atuais
POST   /api/budget/limits                → set caps (hora/dia/mês)

# WebSocket
WS     /ws                               → eventos em tempo real:
                                            instance_status_changed,
                                            task_transitioned,
                                            new_discovered_item,
                                            budget_warning, budget_breached

# Webhooks (chamados por GitHub/Jira/Sentry)
POST   /webhooks/github                  → PR merged, comment, etc.
POST   /webhooks/jira                    → status change
POST   /webhooks/sentry                  → novo issue

# Health
GET    /healthz, /readyz, /metrics       → Prometheus
```

### 4.3. Dependências novas

Adicionar em `pyproject.toml`:
```toml
"fastapi>=0.110",
"uvicorn[standard]>=0.30",
"websockets>=12",
"apscheduler>=3.10",
"prometheus-client>=0.20",
"anthropic>=0.40",         # só pra contagem de tokens / pricing helper
```

### 4.4. Como subir

Nova entrada no `pyproject.toml`:
```toml
[project.scripts]
flowtrack = "flowtrack.main:app"
flowtrack-server = "flowtrack.api.app:run"   # NOVO
```

E novo serviço no `docker-compose.yml`:
```yaml
api:
  build: .
  depends_on:
    db: { condition: service_healthy }
    migrate: { condition: service_completed_successfully }
  environment:
    FLOWTRACK_DATABASE_URL: postgresql://flowtrack:flowtrack@db:5432/flowtrack
    FLOWTRACK_ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
  ports: ["8080:8080"]
  command: ["uv", "run", "flowtrack-server"]
  volumes:
    - ./worktrees:/worktrees          # persiste worktrees
    - /var/run/docker.sock:/var/run/docker.sock  # se for spawnar em containers depois
```

---

## 5. Núcleo do orquestrador

### 5.1. Loop principal (`flowtrack/orchestrator/loop.py`)

Pseudocódigo:

```python
async def orchestrator_loop():
    while not shutdown_event.is_set():
        # 1. Watchdog: mata instâncias zumbis
        await watchdog.sweep()      # last_heartbeat_at > now - 2min -> kill

        # 2. Budget gate: se janela atual estourou, pula spawn novo
        if await budget.exhausted(window='hour'):
            await asyncio.sleep(30)
            continue

        # 3. Concorrência: respeita limite global (config.max_concurrent_instances)
        active = await instance_repo.count_active()
        if active >= settings.max_concurrent_instances:
            await asyncio.sleep(2)
            continue

        # 4. Pega próximo job da fila com SKIP LOCKED
        job = await job_queue.claim_next()
        if not job:
            await asyncio.sleep(2); continue

        # 5. Tenta adquirir locks de recursos que a task precisa
        locks = derive_locks_for_task(job.task)   # ex.: módulo, arquivos previstos
        if not await resource_locks.acquire_all(locks, instance_placeholder_id):
            await job_queue.requeue(job, delay=60)  # tenta de novo em 1min
            continue

        # 6. Spawna instância
        await spawner.spawn(job, locks)
```

`spawner.spawn(job, locks)` faz:
1. Cria row em `instances` (status='spawning').
2. Cria worktree git: `git worktree add /worktrees/<instance_id> -b auto/<task_id>-<role>`.
3. Monta prompt a partir de `roles.system_prompt` + `tasks.title/description` + `acceptance_criteria` + payload do job.
4. Roda subprocess:
   ```
   claude --print \
     --session-id <claude_session_id> \
     --output-format stream-json \
     --append-system-prompt "<role.system_prompt>" \
     --allowed-tools "<role.tools_allowed>" \
     --cwd /worktrees/<instance_id> \
     --max-turns 50 \
     -- "<prompt rendered>"
   ```
5. Parseia stdout (stream-json) em `stream_parser`, grava em `instance_events`, atualiza
   `instances.tokens_input/output`, manda evento via WebSocket pro frontend.
6. Quando subprocess termina: marca `instances.status=completed|failed`, libera locks,
   muda status da task pro próximo estágio (regra do papel: dev → in_review; reviewer → in_qa; qa → done).
7. Se status novo for "in_review", "in_qa" etc., orchestrator enfileira próximo job
   automaticamente pro papel seguinte (ver §6.3).

### 5.2. Heartbeat

Cada instância de Claude precisa reportar "estou vivo" pro daemon a cada ~30s. Duas
opções:

**Opção A (Recomendada):** o stream-json já emite eventos contínuos enquanto o agent
trabalha. O `stream_parser` interpreta qualquer evento recebido como heartbeat e
atualiza `last_heartbeat_at = now()`. Zero código no lado do Claude.

**Opção B:** hook `PreToolUse` configurado em `.claude/settings.json` da worktree
chama `curl http://daemon:8080/api/instances/<id>/heartbeat`. Mais explícito mas
exige hook setup por worktree.

### 5.3. Locking lógico

Por que: dois agents Dev mexendo no mesmo arquivo geram merge conflict garantido.

**Granularidade do lock — recomendado começar grosso e refinar:**

| Granularidade | Quando |
|---------------|--------|
| Por **módulo top-level** (ex.: `flowtrack/services/`) | **MVP** — simples, conservador |
| Por **arquivo** | Fase 4+, exige PM agent declarar arquivos previstos |
| Por **package npm/python** | Pra repos polyglot |
| Por **migration** | Sempre — só 1 instância pode rodar migration ao mesmo tempo |

A função `derive_locks_for_task(task)` usa:
1. `tasks.module_hint` se presente (declarado por PM agent ou humano).
2. Senão, heurística: extrai paths de `tasks.description` (regex de `flowtrack/...`)
   e mapeia pra módulos.
3. Senão, lock genérico `module:_unknown_` (= efetivamente serializa execução).

Locks são renovados a cada heartbeat. Expiram em `acquired_at + role.max_minutes`.
Se expira sem renovação: outra instância pode pegar (a anterior já é zumbi nessa altura).

### 5.4. Circuit breaker e budget

`flowtrack/orchestrator/budget.py` enforce em 3 níveis:

1. **Por instância:** `role.max_tokens` e `role.max_minutes`. Excedeu → kill.
2. **Por janela (hora/dia/mês):** `budget_windows.cost_usd`. Excedeu → bloqueia spawn novo.
3. **Por task:** se uma task acumulou > N tentativas falhadas, marca `tasks.status='blocked'` e cria comment "needs human review".

Default seguro recomendado pra começar:
- max_concurrent_instances: **2**
- role.max_minutes (dev): **30**, (reviewer): **15**, (qa): **20**
- budget.day_cap_usd: **$25**
- budget.hour_cap_usd: **$5**

Esses valores ficam em `config` (encriptados ou não) e podem mudar via `/api/budget/limits`.

---

## 6. Integração com Claude Code

### 6.1. Modo headless

Claude Code suporta execução não-interativa via `--print` (alias: `-p`). Flags
relevantes:

```
claude --print "prompt"
  --session-id <uuid>           # resumir/inspecionar depois
  --output-format stream-json   # eventos linha-por-linha (jsonl)
  --max-turns N                 # hard limit de turnos
  --append-system-prompt TEXT   # injeta system prompt do papel
  --allowed-tools T1,T2,...     # whitelist (Read,Edit,Bash,Grep,etc.)
  --cwd PATH                    # diretório de trabalho (= worktree)
  --resume <session-id>         # continua sessão anterior
```

### 6.2. Parsing do stream-json

Cada linha é um JSON com formato:
```json
{"type":"tool_use","tool":"Edit","params":{...}}
{"type":"message","role":"assistant","content":"..."}
{"type":"usage","input_tokens":1234,"output_tokens":567,"cache_read_tokens":0}
{"type":"error","message":"..."}
{"type":"result","exit_reason":"end_turn"}
```

`stream_parser.py`:
- Para cada linha: grava em `instance_events`, atualiza tokens/cost, broadcast no WebSocket.
- Eventos `result` ou `error` finais decidem `instances.status` final.
- Eventos `tool_use` mostram no kanban "fazendo Edit em X" (UX feedback).

### 6.3. Pipeline de papéis e transições

Quando uma instância termina sucesso, orchestrator enfileira próximo papel. Mapa
default:

```
Discovery item promovido → tasks(status=todo, acceptance_criteria=NULL)
  ↓ enqueue role=pm
PM agent: refina, escreve acceptance_criteria, define module_hint
  ↓ task vira status=todo com acceptance_criteria preenchido (= "Ready")
  ↓ enqueue role=dev
Dev agent: cria branch, implementa, commita, abre PR
  ↓ task vira status=in_review
  ↓ enqueue role=reviewer
Reviewer agent: revisa diff do PR, comenta, aprova ou rejeita
  ↓ se aprovado: status=in_qa
  ↓ enqueue role=qa
QA agent: roda testes, smoke test, fuzz curto
  ↓ se passou: status=done (e abre merge — humano aprova merge OU automerge se config permitir)
```

Transições rejeitadas voltam pro papel anterior (reviewer rejeita → volta pra dev
com comentários como contexto).

### 6.4. Skills do FlowTrack vivendo na worktree

Cada worktree spawnada precisa que as skills do FlowTrack estejam disponíveis pro
Claude rodando lá. Opções:

**Recomendado:** copiar `.claude/` (que vamos criar — ver §10) pra `<worktree>/.claude/`
no momento do `worktree add`. Isso garante que os comandos `/ft-*` e `/implement` funcionam.

Alternativa: configurar em `~/.claude/` do usuário que roda o daemon. Mais frágil
(depende do ambiente do daemon).

### 6.5. Hooks da worktree reportando ao daemon

Em `<worktree>/.claude/settings.json` (gerado no spawn):

```json
{
  "hooks": {
    "Stop": [
      {"command": "curl -X POST http://localhost:8080/api/instances/$FLOWTRACK_INSTANCE_ID/finished -d @-"}
    ],
    "SubagentStop": [
      {"command": "curl -X POST http://localhost:8080/api/instances/$FLOWTRACK_INSTANCE_ID/subagent-done -d @-"}
    ]
  }
}
```

Env var `FLOWTRACK_INSTANCE_ID` é injetada no subprocess pelo spawner.

---

## 7. Modelo de papéis

### 7.1. Catálogo inicial (seed via migration ou comando)

| Role | Quando entra | Ferramentas |
|------|--------------|-------------|
| `pm` | Após discovery promoção | Read, Grep, WebFetch, Edit (só em descrição da task) |
| `po` | Decide prioridade no backlog | Read (banco via API), nenhum Edit |
| `design` | Tasks com label `ui` | Read, Write (cria mockups em /docs/design/) |
| `dev` | Task com criteria preenchido | Read, Edit, Write, Bash, Grep, Glob |
| `reviewer` | Após PR aberto | Read, Grep, Bash (só git/test) |
| `qa` | Após approve do reviewer | Read, Bash, Edit (em testes apenas) |
| `devops` | Falha em CI ou deploy | Bash, Read, Edit em .github/ |

Cada role tem `system_prompt` armazenado em `roles.system_prompt` (text). Exemplo
pra `dev`:

```
Você é um desenvolvedor sênior trabalhando em uma task isolada.

CONTEXTO:
- Você está em uma git worktree separada do repo.
- A task atual te diz O QUÊ implementar; o "acceptance_criteria" diz CRITÉRIO DE PRONTO.
- Você só pode editar arquivos dentro dos módulos listados em "module_hint".

REGRAS DURAS:
1. Não faça merges. Apenas commit + push da branch atual.
2. Não rode `git rebase`, `git reset --hard`, `--force` em nada.
3. Se precisar de algo fora do module_hint, registre comentário na task e termine.
4. Todo commit deve ter mensagem no formato: "<task_ticket>: <verbo no imperativo>".
5. Antes de terminar: rode os testes (`pytest`) e mostre o resultado.

ENTREGA:
- Push da branch.
- Comentário final na task com link do PR e um resumo de 3 linhas do que mudou.
```

Prompts vão evoluir; mantê-los em DB (não em arquivo) permite ajustar sem deploy.

### 7.2. PM agent — caso especial

PM não roda Claude Code com tool use forte. Ele faz **só leitura + escrita em DB**.
Implementação realista: pode ser função Python que **chama a API Anthropic
diretamente** (não Claude Code) pra refinar uma `discovered_item` em `task`. Mais
barato e previsível que spawnar uma instância completa.

Trade: PM agent **não usa o pipeline de instâncias**. Vive em `flowtrack/agents/pm.py`
como serviço async chamado pelo orchestrator quando uma discovered_item é promovida.

Mesmo vale pra `po` (decisão de priorização — função puramente analítica) e `design`
fica pra fase 6+.

---

## 8. Frontend kanban

### 8.1. Stack recomendada

- **Next.js 15 (App Router)** + **TanStack Query** + **shadcn/ui**
- Mora num diretório separado: `C:/workspace/flowtrack-web/` (repo próprio) OU
  `flowtrack/web/` (monorepo).
- **Recomendado:** repo próprio. Frontend evolui em ciclo diferente do backend.

### 8.2. Telas

1. **Kanban** (`/`) — colunas Discovery → Refinement → Ready → In Progress → Blocked → In Review → QA → Merged.
   Cards mostram: título, ticket Jira, role atual, avatar da instância, último evento (ex.: "editando services/sync_service.py"), tempo na coluna.
   Drag-and-drop entre colunas (com confirmação se for transição "não natural").

2. **Instance detail** (`/instances/{id}`) — log de eventos em tempo real (WS), tokens consumidos, custo, botões Kill / Interrupt / Resume.

3. **Discovery inbox** (`/discovery`) — lista de items descobertos com botões Promote / Reject / Merge with existing.

4. **Budget** (`/budget`) — gráfico de custo por hora/dia, cap atual, instâncias mais caras.

5. **Settings** (`/settings`) — config de roles (system_prompt, tools_allowed, limites), discovery sources, integrações.

### 8.3. Update em tempo real

Frontend abre WebSocket em `/ws`, recebe eventos e atualiza cache do TanStack Query.
Para eventos críticos (instance terminou, budget breached) mostra toast.

### 8.4. Não fazer no MVP de frontend

- Drag-and-drop entre colunas (forçar via dropdown).
- Multi-tenant.
- Mobile.
- Atalhos de teclado fancy.
- Dark mode (use o que o shadcn dá grátis e segue).

---

## 9. Descoberta automática de tarefas

### 9.1. Interface de fonte

```python
# flowtrack/discovery/base.py
class DiscoverySource(Protocol):
    name: str
    interval_seconds: int

    async def fetch(self) -> list[DiscoveryCandidate]: ...
```

`DiscoveryCandidate` vira `discovered_items` row se `source_ref` ainda não existe.

### 9.2. Fontes do MVP (em ordem de implementação)

1. **`jira_backlog`** — fácil. Pega issues do Jira com label `auto` e que não tenham
   `tasks.ticket_id` correspondente no banco. Cria `discovered_item` source='jira'.
   Recomendado começar **só** com isso na fase 1.

2. **`pr_comments`** — média. GitHub API: lista comentários em PRs mergeados nos
   últimos 7 dias com padrão "TODO:", "follow-up:", "please fix in next PR". Já
   estruturado.

3. **`sentry`** — média. Sentry API: groups com event_count > X nas últimas 24h, agrupados
   por fingerprint. Risco alto de gerar bug fictício se o erro for transitório —
   precisa de threshold conservador.

4. **`github_issues`** — fácil. Issues abertas com label `bot-pickup`. Praticamente
   igual ao jira_backlog.

5. **`log_pattern`** — adiar pra fase 7+. Requer parsing de logs próprios e é
   barulhento.

### 9.3. Scheduler

`flowtrack/discovery/scheduler.py` usa APScheduler:
```python
scheduler.add_job(jira_source.run, 'interval', minutes=10)
scheduler.add_job(sentry_source.run, 'interval', minutes=15)
# etc.
```

Cada `run()` é idempotente (UNIQUE em `(source, source_ref)`).

### 9.4. Dedupe e ranking

Se 3 fontes reportarem o "mesmo bug" (heurística: similaridade de título > 0.8),
mesclar na `discovered_items` mais antiga e somar `signal_score`.

PM agent (ou humano) decide promote/reject. Default conservador: **nada vira task
automaticamente sem aprovação humana** até ter 4+ semanas de dados mostrando que o
PM agent não cria lixo.

---

## 10. Mudanças nas skills existentes

As skills atuais (`/ft-status`, `/ft-config`, `/ft-sync`, `/implement`, `/plan`,
`/task`, `/review`, `/test`, `/deploy`, `/incident`, `/interrupt`, `/report`) hoje
chamam `flowtrack` CLI diretamente via Bash. Continuam funcionando — modo
"single-developer" não muda.

**O que muda:**

### 10.1. Criar `flowtrack/.claude/` no repo

Hoje vazio. Adicionar:

```
.claude/
├── settings.json                ← config de hooks pra dev local
├── commands/                    ← slash commands específicos do repo
│   ├── ft-orchestrator.md       ← /ft-orchestrator status|start|stop|kill <id>
│   ├── ft-instance.md           ← /ft-instance logs <id> / kill <id>
│   └── ft-discover.md           ← /ft-discover refresh / promote <id>
└── agents/                      ← subagents internos (review, qa)
    ├── reviewer.md
    └── qa.md
```

### 10.2. Skills do FlowTrack: adicionar "modo orchestrator"

Cada skill (`implement`, `review`, `test`, etc.) hoje assume "humano + 1 sessão local".
Pra modo orquestrado precisa detectar via env var:

```bash
if [ -n "$FLOWTRACK_INSTANCE_ID" ]; then
  # modo orchestrator: reporta progresso via API
  flowtrack-internal-report --instance "$FLOWTRACK_INSTANCE_ID" --stage "starting"
else
  # modo CLI normal: comportamento atual
fi
```

Mudança pequena por skill (~10 linhas). Não quebra uso atual.

### 10.3. Novo comando CLI: `flowtrack serve` e `flowtrack worker`

- `flowtrack serve` — sobe o daemon FastAPI.
- `flowtrack worker` — modo "só workers, sem API" (pra rodar em outra máquina se algum dia escalar horizontalmente).
- `flowtrack orchestrator status` — atalho pra GET /api/instances.

---

## 11. Segurança e safety

### 11.1. Garantias duras (configurar uma vez, nunca relaxar)

1. **Worktrees são isoladas.** Uma instância não pode `cd` pra fora da worktree.
   Reforçar via `--cwd` + system prompt + revisão dos `tools_allowed`.
2. **Branch protection no remote.** `main` exige PR + approval humano OU approval
   de role=reviewer + role=qa (configurável). Sem isso, dev agent pode pushar pra main.
3. **Sem `--dangerously-skip-permissions` nunca** em modo orchestrator. Tools
   sensíveis (Bash com `rm`, `git push --force`) ficam fora do `tools_allowed`.
4. **Tokens Anthropic em vault.** Reaproveitar `flowtrack/core/crypto.py`. Nunca
   logar no `instance_events`.
5. **Network egress controlado.** Se rodar em produção real, daemon roda em rede que
   bloqueia egress pra qualquer coisa que não seja Anthropic API, GitHub, Jira, Sentry.

### 11.2. Garantias soft (configuráveis)

1. **`role.max_minutes`** — hard kill por papel.
2. **`role.max_tokens`** — hard kill por consumo.
3. **`budget_windows.*_cap_usd`** — bloqueia spawn novo.
4. **Falha em N tentativas** → task vai pra `blocked` e cria comentário pra humano.

### 11.3. Detecção de "agent travado"

Sintomas a monitorar (alertas via webhook em config):
- Instância > 80% do max_minutes sem evento `tool_use` nos últimos 5min → kill.
- Instância repetindo mesmo tool_use 5x seguidas (loop) → kill.
- Custo de uma única task > 3x média histórica → kill + alerta.

### 11.4. Auditoria

Tudo que uma instância fez está em `instance_events`. Combinado com git log da
worktree, é trilha completa. Retenção: 30 dias por default; bumpar pra 90 se for
ambiente regulado.

---

## 12. Observabilidade

- **`/metrics` Prometheus:** active_instances, queue_depth, cost_usd_total, tokens_total_per_role, task_cycle_time_seconds (histogram), failures_total.
- **Logs estruturados (JSON):** uvicorn já dá; padronizar com `structlog`.
- **Tracing (opcional):** OpenTelemetry instrumentation no FastAPI + httpx pra ver chamadas pra Anthropic/Jira/GitHub.
- **Dashboards:** Grafana com 2 painéis: Operations (instâncias, fila, falhas) e Cost (gasto por papel, por task, projeção).

---

## 13. Migração incremental (roadmap)

### Fase 1 — "Hello, autonomous dev" (2–3 semanas)
- Migration 005 (todas tabelas novas).
- FastAPI app com 3 endpoints: GET /api/kanban, POST /api/tasks/{id}/assign, GET /api/instances.
- Orchestrator loop **single-threaded** rodando 1 instância por vez.
- Apenas role `dev`.
- Apenas fonte de discovery: `jira_backlog`.
- Sem frontend ainda — usar `curl` + `/ft-orchestrator status` slash command.
- **Critério de pronto:** dar uma task "adicione campo X ao model Y", agent abre PR sozinho, humano faz merge.

### Fase 2 — Frontend mínimo (1–2 semanas)
- Next.js com tela `/` (kanban readonly) e `/instances/{id}` (logs WS).
- Sem drag-and-drop. Sem settings UI.
- **Critério:** dá pra acompanhar visualmente a fase 1 sem ficar olhando log.

### Fase 3 — Múltiplas instâncias + locks (2 semanas)
- `max_concurrent_instances = 3`.
- `resource_locks` por módulo.
- Watchdog.
- Budget enforcement com cap diário.
- **Critério:** 3 tasks em paralelo em módulos diferentes sem conflito.

### Fase 4 — Reviewer agent (2 semanas)
- Role `reviewer` com prompt que lê diff e comenta no PR.
- Pipeline dev → in_review → (humano aprova merge).
- **Critério:** PR aberto pelo dev agent recebe comentário do reviewer agent antes do humano olhar.

### Fase 5 — QA agent + Sentry discovery (3 semanas)
- Role `qa` que roda testes na worktree e abre comments.
- Fonte `sentry` com threshold conservador.
- **Critério:** bug do Sentry com event_count > 50 vira discovered_item; humano promove; dev fixa; reviewer revisa; qa testa.

### Fase 6 — PM/PO agents (3–4 semanas)
- PM agent refina discovered_item em task com acceptance_criteria.
- PO agent prioriza fila.
- **Critério:** ciclo discovery → refinement → ready → ... sem humano até "in_review".

### Fase 7 — 24/7 hardening (3 semanas)
- Auto-restart do daemon (systemd ou docker restart=always).
- Retry com backoff em falhas transitórias de API Anthropic.
- Budget breach pause + auto-resume na próxima janela.
- Notificações Slack pra falhas/budget warnings.
- **Critério:** o daemon roda 7 dias sem intervenção manual sem produzir lixo.

---

## 14. O que NÃO fazer no MVP

Lista explícita pra resistir à tentação:

- ❌ Celery / Redis. Postgres + `SKIP LOCKED` resolve até ~50 jobs/h.
- ❌ Kubernetes. Docker-compose num único host até ter motivo real.
- ❌ Multi-tenant. Um time, um banco.
- ❌ Frontend com drag-and-drop entre colunas. Dropdown serve.
- ❌ "Agent que escreve seu próprio prompt." Prompts são versionados em DB com migrations manuais.
- ❌ Auto-merge sem aprovação humana. Até fase 7.
- ❌ Design agent. Praticamente todo time real lida com isso fora do escopo de "agent code".
- ❌ Subagents escolhendo qual subagent invocar dinamicamente. Pipeline é fixo até comprovar o oposto.
- ❌ Treinar/finetunar modelo próprio. Não há ganho que justifique o custo.

---

## 15. Riscos conhecidos (e o que fazer com eles)

| Risco | Probabilidade | Impacto | Mitigação |
|-------|---------------|---------|-----------|
| Dois dev agents fazem merge conflict | Alta | Médio | Locks por módulo (§5.3) + branch isolada |
| Reviewer agent aprova código ruim | **Muito alta** | Alto | Humano aprova merge até fase 7; manter checklist explícito no prompt |
| Discovery cria 100 tasks duplicadas de um bug ruidoso | Alta | Médio | UNIQUE (source, source_ref) + similaridade de título pra dedupe |
| Custo explode (instância em loop) | Alta | Alto | `max_tokens`, `max_minutes`, budget cap, detecção de loop (§11.3) |
| Agent vaza secret em log | Média | Alto | Tokens via env var (não args), redaction no `stream_parser` |
| Frontend dessincroniza do estado real | Alta | Baixo | WS reconnect + polling fallback de 30s |
| Migration corre em paralelo em duas instâncias | Baixa | **Muito alto** | Lock global `migration` (§5.3) |
| Hooks do Claude Code mudam de formato | Baixa | Médio | Pinning de versão do Claude Code, monitorar release notes |
| Anthropic API tem outage | Baixa | Alto | Retry com backoff; budget paga "instância parada"; alarme |
| PM agent escreve acceptance_criteria errado, dev implementa coisa errada | **Muito alta** | Médio | Humano aprova promoção de discovered_item → task até fase 6 estável |

---

## 16. Decisões em aberto (precisam de input antes de começar)

- **Repo do frontend:** separado ou monorepo? _Recomendação: separado._
- **Onde rodar o daemon:** local 24/7 (Windows/WSL2)? VPS Linux? _Recomendação: VPS Linux com restart=always._
- **Quem aprova merge:** humano sempre? Só fora de horário comercial? _Recomendação: humano sempre até fase 7._
- **Modelo padrão:** Sonnet 4.6 vs Opus 4.7? _Recomendação: Sonnet pra dev/reviewer/qa (custo); Opus pra pm/po (decisão mais cara, executa menos vezes)._
- **Limite de custo aceito por dia:** $25 é chute. Quanto você está disposto a queimar antes do circuit breaker?
- **Repositórios alvo:** só este repo? Múltiplos? _Recomendação: começar com 1 (este mesmo)._

---

## 17. Próximos passos concretos

1. **Decidir as 6 questões em §16.**
2. **Criar branch `feat/orchestrator-foundation`.**
3. **Implementar migration 005 + modelos novos** (não engata em nada ainda; só schema).
4. **Implementar `flowtrack serve` com 1 endpoint:** `GET /api/kanban` (read-only do que já existe).
5. **Implementar orchestrator loop em modo "dry run":** loga o que faria, não spawna.
6. **Primeiro spawn real:** 1 task hardcoded, 1 role (`dev`), worktree, claude headless, esperar terminar.
7. **A partir daí, seguir o roadmap §13.**

---

**Última revisão:** 2026-05-23.
**Mantenedor deste doc:** atualizar a cada fase concluída, marcando o que mudou de
plano (sempre acontece).
