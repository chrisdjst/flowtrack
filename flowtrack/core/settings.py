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
    auto_sync: bool = True


settings = Settings()
