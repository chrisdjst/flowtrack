from __future__ import annotations

from flowtrack.core.settings import settings

SENSITIVE_KEYS = frozenset({"github_token", "jira_token"})

DB_KEYS = frozenset({
    "github_token",
    "github_owner",
    "github_repo",
    "jira_base_url",
    "jira_email",
    "jira_token",
    "jira_project_key",
})


def get_credential(key: str) -> str:
    """Get a credential. Env var takes priority, then DB (decrypted)."""
    env_value = getattr(settings, key, "")
    if env_value:
        return env_value

    if key not in DB_KEYS:
        return ""

    from flowtrack.core.database import get_db
    from flowtrack.services.config_service import ConfigService

    try:
        with get_db() as db:
            svc = ConfigService(db)
            return svc.get(key) or ""
    except Exception:
        return ""


def load_credentials(*keys: str) -> dict[str, str]:
    """Load multiple credentials at once (single DB connection)."""
    result: dict[str, str] = {}
    db_keys: list[str] = []

    for key in keys:
        env_value = getattr(settings, key, "")
        if env_value:
            result[key] = env_value
        elif key in DB_KEYS:
            db_keys.append(key)
        else:
            result[key] = ""

    if db_keys:
        from flowtrack.core.database import get_db
        from flowtrack.services.config_service import ConfigService

        try:
            with get_db() as db:
                svc = ConfigService(db)
                for key in db_keys:
                    result[key] = svc.get(key) or ""
        except Exception:
            for key in db_keys:
                result.setdefault(key, "")

    return result
