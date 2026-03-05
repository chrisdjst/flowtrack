from sqlalchemy.orm import Session as DbSession

from flowtrack.repositories.config_repo import ConfigRepository


class ConfigService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.repo = ConfigRepository(db)

    def get(self, key: str) -> str | None:
        return self.repo.get(key)

    def set(self, key: str, value: str) -> None:
        self.repo.set(key, value)

    def get_all(self) -> dict[str, str]:
        configs = self.repo.get_all()
        return {c.key: c.value for c in configs}

    def delete(self, key: str) -> bool:
        return self.repo.delete(key)
