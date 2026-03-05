# FlowTrack

CLI para captura de métricas de produtividade baseada nos frameworks **SPACE** e **DORA**, com integração ao **GitHub** e **Jira**.

## O que é medido?

### SPACE Metrics
- **Satisfaction & Well-being** — tempo em flow vs. tempo bloqueado
- **Performance** — sessões por dia, duração de code reviews
- **Activity** — sessões de desenvolvimento, review e testes
- **Communication & Collaboration** — interrupções por sessão (Slack, reuniões)
- **Efficiency & Flow** — flow time ratio, blocking ratio

### DORA Metrics
- **Deployment Frequency** — frequência de deploys por dia
- **Lead Time for Changes** — tempo entre commit e deploy
- **Change Failure Rate** — taxa de falha após deploy
- **Mean Time to Recovery (MTTR)** — tempo médio de resolução de incidentes

## Pré-requisitos

- Python 3.12+
- PostgreSQL
- [uv](https://docs.astral.sh/uv/) (gerenciador de pacotes)

## Instalação

```bash
# Clone o repositório
git clone https://github.com/chrisdjst/flowtrack.git
cd flowtrack

# Instale as dependências com uv
uv sync

# Instale as dependências de desenvolvimento
uv sync --group dev
```

## Configuração

### 1. Variáveis de ambiente

Copie o arquivo de exemplo e preencha com suas credenciais:

```bash
cp .env.example .env
```

| Variável | Descrição |
|---|---|
| `FLOWTRACK_DATABASE_URL` | URL de conexão PostgreSQL (padrão: `postgresql://localhost:5432/flowtrack`) |
| `FLOWTRACK_GITHUB_TOKEN` | Personal access token do GitHub |
| `FLOWTRACK_GITHUB_OWNER` | Owner do repositório GitHub |
| `FLOWTRACK_GITHUB_REPO` | Nome do repositório GitHub |
| `FLOWTRACK_JIRA_BASE_URL` | URL base do Jira (ex: `https://empresa.atlassian.net`) |
| `FLOWTRACK_JIRA_EMAIL` | Email da conta Jira |
| `FLOWTRACK_JIRA_TOKEN` | API token do Jira |
| `FLOWTRACK_AUTO_SYNC` | Sync automático ao encerrar sessão (padrão: `true`) |

### 2. Banco de dados

Crie o banco PostgreSQL e rode as migrações:

```bash
createdb flowtrack
uv run alembic upgrade head
```

### 3. Configuração interativa

Alternativamente, configure credenciais via CLI:

```bash
uv run flowtrack config
```

Para visualizar a configuração atual:

```bash
uv run flowtrack config --show
```

## Uso

### Sessões de desenvolvimento

```bash
# Iniciar sessão (opcionalmente vinculando ticket/PR)
uv run flowtrack dev start
uv run flowtrack dev start --ticket PROJ-123 --pr 42

# Pausar / retomar
uv run flowtrack dev pause
uv run flowtrack dev resume

# Encerrar sessão
uv run flowtrack dev end
uv run flowtrack dev end --no-sync  # sem sincronizar
```

### Code review

```bash
uv run flowtrack review start --pr 42
uv run flowtrack review end
```

### Testes

```bash
uv run flowtrack test start --ticket PROJ-123
uv run flowtrack test end
```

### Bloqueios

```bash
uv run flowtrack block start --reason "Aguardando aprovação"
uv run flowtrack block end
```

### Interrupções

```bash
uv run flowtrack interrupt start --type meeting
uv run flowtrack interrupt start --type slack
uv run flowtrack interrupt end
```

### Deployments

```bash
uv run flowtrack deploy --env production
uv run flowtrack deploy --env staging
```

### Incidentes

```bash
uv run flowtrack incident start --description "API fora do ar"
uv run flowtrack incident end
```

### Status

```bash
uv run flowtrack status
```

Exibe um painel com a sessão ativa, tempo decorrido, bloqueios e interrupções.

### Sync manual

```bash
uv run flowtrack sync
```

Sincroniza dados da sessão ativa com GitHub e Jira.

### Relatórios

```bash
uv run flowtrack report
uv run flowtrack report --period week
uv run flowtrack report --period month
uv run flowtrack report --period sprint
```

Gera tabelas com as métricas SPACE e DORA para o período selecionado.

## Testes

```bash
# Rodar todos os testes
uv run pytest

# Com cobertura
uv run pytest --cov=flowtrack
```

## Estrutura do projeto

```
flowtrack/
├── cli/               # Comandos CLI (Typer)
│   ├── block.py       # Gerenciamento de bloqueios
│   ├── config.py      # Configuração interativa
│   ├── deploy.py      # Registro de deploys
│   ├── dev.py         # Sessões de desenvolvimento
│   ├── incident.py    # Gerenciamento de incidentes
│   ├── interrupt.py   # Registro de interrupções
│   ├── report.py      # Geração de relatórios
│   ├── review.py      # Sessões de code review
│   ├── status.py      # Status da sessão ativa
│   ├── sync.py        # Sync manual GitHub/Jira
│   └── test_cmd.py    # Sessões de teste
├── core/              # Infraestrutura
│   ├── console.py     # Console Rich
│   ├── database.py    # Conexão com banco
│   ├── exceptions.py  # Exceções customizadas
│   └── settings.py    # Configurações (Pydantic)
├── integrations/      # Integrações externas
│   ├── github_client.py
│   └── jira_client.py
├── models/            # Modelos SQLAlchemy
├── repositories/      # Camada de acesso a dados
├── services/          # Lógica de negócio
└── main.py            # Entry point CLI
```

## Tecnologias

- **CLI**: [Typer](https://typer.tiangolo.com/) + [Rich](https://rich.readthedocs.io/)
- **ORM**: [SQLAlchemy](https://www.sqlalchemy.org/) 2.0
- **Migrações**: [Alembic](https://alembic.sqlalchemy.org/)
- **HTTP Client**: [httpx](https://www.python-httpx.org/)
- **Configuração**: [Pydantic Settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
- **Banco de dados**: PostgreSQL
- **Testes**: pytest + pytest-cov
- **Linting**: Ruff
- **Type checking**: mypy (strict)

## Licença

MIT
