from sqlalchemy.orm import Session as DbSession

from flowtrack.core.credentials import SENSITIVE_KEYS
from flowtrack.core.crypto import CryptoService
from flowtrack.models.config import Config
from flowtrack.repositories.config_repo import ConfigRepository


class ConfigService:
    def __init__(self, db: DbSession) -> None:
        self.db = db
        self.repo = ConfigRepository(db)
        self.crypto = CryptoService()

    def get(self, key: str) -> str | None:
        config = self.repo.get_raw(key)
        if not config:
            return None
        if config.encrypted:
            return self.crypto.decrypt(config.value)
        return config.value

    def set(self, key: str, value: str) -> None:
        encrypted = key in SENSITIVE_KEYS
        stored_value = self.crypto.encrypt(value) if encrypted else value
        self.repo.set(key, stored_value, encrypted=encrypted)

    def get_all(self) -> dict[str, str]:
        configs = self.repo.get_all_raw()
        result: dict[str, str] = {}
        for c in configs:
            if c.encrypted:
                result[c.key] = self.crypto.decrypt(c.value)
            else:
                result[c.key] = c.value
        return result

    def get_all_raw(self) -> list[Config]:
        return self.repo.get_all_raw()

    def delete(self, key: str) -> bool:
        return self.repo.delete(key)

    def encrypt_existing(self) -> int:
        """Encrypt any plaintext sensitive values. Returns count of encrypted keys."""
        configs = self.repo.get_all_raw()
        count = 0
        for config in configs:
            if config.key in SENSITIVE_KEYS and not config.encrypted and config.value:
                config.value = self.crypto.encrypt(config.value)
                config.encrypted = True
                count += 1
        if count:
            self.db.flush()
        return count

    def rotate_key(self, new_crypto: CryptoService) -> int:
        """Re-encrypt all encrypted values with a new key. Returns count."""
        configs = self.repo.get_all_raw()
        count = 0
        for config in configs:
            if config.encrypted and config.value:
                plaintext = self.crypto.decrypt(config.value)
                config.value = new_crypto.encrypt(plaintext)
                count += 1
        if count:
            self.db.flush()
        self.crypto = new_crypto
        return count
