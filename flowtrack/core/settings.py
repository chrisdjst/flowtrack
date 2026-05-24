from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "FLOWTRACK_", "env_file": ".env", "extra": "ignore"}

    database_url: str = "postgresql://localhost:5432/flowtrack"
    github_token: str = ""
    github_owner: str = ""
    github_repo: str = ""
    jira_base_url: str = ""
    jira_email: str = ""
    jira_token: str = ""
    jira_project_key: str = ""
    auto_sync: bool = True

    # ----- Orchestrator -----
    api_host: str = "127.0.0.1"
    api_port: int = 8080
    # Dry run: orchestrator claims jobs from queue but does not spawn Claude Code.
    # Useful for verifying queue/locks/budget plumbing without burning tokens.
    orchestrator_dry_run: bool = True
    orchestrator_loop_interval_seconds: float = 2.0
    max_concurrent_instances: int = 2
    # Where worktrees are created. Per-instance subdir.
    worktree_root: str = "./worktrees"
    # Repo the dev role checks out. Empty = current working dir.
    target_repo_path: str = ""
    # Executable used to spawn an instance. Override for tests / mocks.
    claude_executable: str = "claude"
    # Passed through to the subprocess as ANTHROPIC_API_KEY. Empty = inherit from daemon env.
    anthropic_api_key: str = ""
    # Hard ceilings independent of role config — safety net.
    instance_global_max_minutes: int = 120
    # Lock TTL when no explicit role.max_minutes is available.
    lock_default_ttl_minutes: int = 30


settings = Settings()
