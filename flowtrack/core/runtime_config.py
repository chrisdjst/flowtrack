"""Runtime configuration: typed settings stored in the `config` DB table.

Priority order (highest → lowest):
  1. Environment variable (via pydantic-settings) — allows CI / deploy overrides
  2. Database row in `config` table — editable at runtime via frontend
  3. Default defined in SETTINGS_SCHEMA

Cache: each key is cached for TTL_SECONDS to avoid a DB round-trip on every
orchestrator tick. The API's PATCH/DELETE handlers call invalidate() so the
new value is visible within one TTL window.
"""

from __future__ import annotations

import time
from typing import Any

from flowtrack.core.settings import settings

TTL_SECONDS = 10.0

# fmt: off
SETTINGS_SCHEMA: dict[str, dict[str, Any]] = {
    # ---------- orchestrator ----------
    "orchestrator_dry_run": {
        "type": "bool", "default": True, "group": "orchestrator", "sensitive": False,
        "description": "When true, jobs are claimed but Claude is never spawned. Set false to go live.",
    },
    "max_concurrent_instances": {
        "type": "int", "default": 2, "group": "orchestrator", "sensitive": False,
        "description": "Maximum number of Claude Code subprocesses running in parallel.",
    },
    "orchestrator_loop_interval_seconds": {
        "type": "float", "default": 2.0, "group": "orchestrator", "sensitive": False,
        "description": "Seconds between orchestrator ticks (job claim attempts).",
    },
    "claude_executable": {
        "type": "str", "default": "claude", "group": "orchestrator", "sensitive": False,
        "description": "Binary / command used to spawn Claude Code. Override for mocks or wrappers.",
    },
    "anthropic_api_key": {
        "type": "str", "default": "", "group": "orchestrator", "sensitive": True,
        "description": "Anthropic API key injected into subprocess env. Empty = inherit from daemon.",
    },
    "worktree_root": {
        "type": "str", "default": "./worktrees", "group": "orchestrator", "sensitive": False,
        "description": "Directory where per-instance git worktrees are created.",
    },
    "target_repo_path": {
        "type": "str", "default": "", "group": "orchestrator", "sensitive": False,
        "description": "Repo the dev role checks out. Empty = current working directory.",
    },
    "instance_global_max_minutes": {
        "type": "int", "default": 120, "group": "orchestrator", "sensitive": False,
        "description": "Hard ceiling for any instance regardless of role config.",
    },
    "lock_default_ttl_minutes": {
        "type": "int", "default": 30, "group": "orchestrator", "sensitive": False,
        "description": "Lock TTL when no explicit role.max_minutes is available.",
    },
    # ---------- budget ----------
    "budget_hour_cap_usd": {
        "type": "float", "default": 0.0, "group": "budget", "sensitive": False,
        "description": "Max spend per hour in USD. 0 = unlimited.",
    },
    "budget_day_cap_usd": {
        "type": "float", "default": 0.0, "group": "budget", "sensitive": False,
        "description": "Max spend per day in USD. 0 = unlimited.",
    },
    # ---------- integrations ----------
    "auto_sync": {
        "type": "bool", "default": True, "group": "integrations", "sensitive": False,
        "description": "Automatically sync session summaries to GitHub/Jira on session end.",
    },
    "github_token": {
        "type": "str", "default": "", "group": "integrations", "sensitive": True,
        "description": "GitHub personal access token (scopes: repo, read:user).",
    },
    "github_owner": {
        "type": "str", "default": "", "group": "integrations", "sensitive": False,
        "description": "GitHub repository owner (user or org slug).",
    },
    "github_repo": {
        "type": "str", "default": "", "group": "integrations", "sensitive": False,
        "description": "GitHub repository name.",
    },
    "jira_base_url": {
        "type": "str", "default": "", "group": "integrations", "sensitive": False,
        "description": "Jira base URL, e.g. https://yourorg.atlassian.net",
    },
    "jira_email": {
        "type": "str", "default": "", "group": "integrations", "sensitive": False,
        "description": "Jira account email for API authentication.",
    },
    "jira_token": {
        "type": "str", "default": "", "group": "integrations", "sensitive": True,
        "description": "Jira API token (generate at id.atlassian.com).",
    },
    "jira_project_key": {
        "type": "str", "default": "", "group": "integrations", "sensitive": False,
        "description": "Jira project key where tasks are created (e.g. KAN).",
    },
    # ---------- discovery ----------
    "github_discovery_label": {
        "type": "str", "default": "bot-pickup", "group": "discovery", "sensitive": False,
        "description": "GitHub issue label that opts an issue into auto-discovery.",
    },
    "jira_discovery_label": {
        "type": "str", "default": "auto", "group": "discovery", "sensitive": False,
        "description": "Jira issue label used in the JQL backlog discovery query.",
    },
    "auto_refine_discovered": {
        "type": "bool", "default": False, "group": "discovery", "sensitive": False,
        "description": "Auto-run PM refinement on every new discovered item (spends tokens).",
    },
}
# fmt: on

_PYTHON_TYPES: dict[str, type] = {"str": str, "int": int, "float": float, "bool": bool}


def _cast(raw: str, type_name: str) -> Any:
    if type_name == "bool":
        return raw.strip().lower() in ("true", "1", "yes", "on")
    return _PYTHON_TYPES[type_name](raw)


class RuntimeConfig:
    """Class-only namespace — no instantiation needed."""

    _cache: dict[str, tuple[Any, float]] = {}

    @classmethod
    def get(cls, key: str) -> Any:
        schema = SETTINGS_SCHEMA.get(key)
        if schema is None:
            raise KeyError(f"RuntimeConfig: unknown key '{key}'")

        # 1. env var (via pydantic-settings). model_fields_set contains only keys that were
        # explicitly set from env/dotenv — never just defaults — so this check is reliable.
        if key in settings.model_fields_set:
            return getattr(settings, key)

        # 2. TTL cache
        cached = cls._cache.get(key)
        if cached is not None:
            value, expires = cached
            if time.monotonic() < expires:
                return value

        # 3. DB
        value = cls._read_db(key, schema)
        cls._cache[key] = (value, time.monotonic() + TTL_SECONDS)
        return value

    @classmethod
    def _read_db(cls, key: str, schema: dict) -> Any:
        try:
            from flowtrack.core.database import get_db
            from flowtrack.services.config_service import ConfigService

            with get_db() as db:
                raw = ConfigService(db).get(key)
            if raw is not None:
                return _cast(raw, schema["type"])
        except Exception:
            pass
        # 4. default
        return schema["default"]

    @classmethod
    def set(cls, key: str, value: str) -> None:
        if key not in SETTINGS_SCHEMA:
            raise KeyError(f"RuntimeConfig: unknown key '{key}'")
        from flowtrack.core.database import get_db
        from flowtrack.services.config_service import ConfigService

        with get_db() as db:
            ConfigService(db).set(key, value)
        cls.invalidate(key)

    @classmethod
    def delete(cls, key: str) -> None:
        if key not in SETTINGS_SCHEMA:
            raise KeyError(f"RuntimeConfig: unknown key '{key}'")
        from flowtrack.core.database import get_db
        from flowtrack.services.config_service import ConfigService

        with get_db() as db:
            ConfigService(db).delete(key)
        cls.invalidate(key)

    @classmethod
    def invalidate(cls, key: str) -> None:
        cls._cache.pop(key, None)

    @classmethod
    def get_all_effective(cls) -> list[dict]:
        """Return all settings with effective value and source for the API."""
        from flowtrack.core.database import get_db
        from flowtrack.services.config_service import ConfigService

        try:
            with get_db() as db:
                db_values: dict[str, str] = ConfigService(db).get_all()  # already decrypted
        except Exception:
            db_values = {}

        result = []
        for key, schema in SETTINGS_SCHEMA.items():
            if key in settings.model_fields_set:
                source = "env"
                effective = str(getattr(settings, key))
            elif key in db_values:
                source = "db"
                effective = db_values[key] or ""
            else:
                source = "default"
                effective = str(schema["default"])

            display_value = "***" if schema["sensitive"] and effective else effective
            result.append({
                "key": key,
                "value": display_value,
                "source": source,
                "type": schema["type"],
                "group": schema["group"],
                "sensitive": schema["sensitive"],
                "description": schema["description"],
                "default": str(schema["default"]),
            })
        return result
