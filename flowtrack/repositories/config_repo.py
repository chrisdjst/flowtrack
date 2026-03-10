from sqlalchemy.orm import Session as DbSession

from flowtrack.models.config import Config


class ConfigRepository:
    def __init__(self, db: DbSession) -> None:
        self.db = db

    def get(self, key: str) -> str | None:
        config = self.db.get(Config, key)
        return config.value if config else None

    def get_raw(self, key: str) -> Config | None:
        return self.db.get(Config, key)

    def set(self, key: str, value: str, encrypted: bool = False) -> Config:
        config = self.db.get(Config, key)
        if config:
            config.value = value
            config.encrypted = encrypted
        else:
            config = Config(key=key, value=value, encrypted=encrypted)
            self.db.add(config)
        self.db.flush()
        return config

    def get_all(self) -> list[Config]:
        return self.db.query(Config).order_by(Config.key).all()

    def get_all_raw(self) -> list[Config]:
        return self.db.query(Config).order_by(Config.key).all()

    def delete(self, key: str) -> bool:
        config = self.db.get(Config, key)
        if config:
            self.db.delete(config)
            self.db.flush()
            return True
        return False
