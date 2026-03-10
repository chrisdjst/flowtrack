import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MASTER_KEY_DIR = Path.home() / ".flowtrack"
MASTER_KEY_PATH = MASTER_KEY_DIR / "master.key"


class CryptoError(Exception):
    pass


class CryptoService:
    """AES-128-CBC + HMAC-SHA256 encryption via Fernet."""

    def __init__(self, key_path: Path = MASTER_KEY_PATH) -> None:
        self._key_path = key_path
        self._fernet = Fernet(self._load_or_create_key())

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as e:
            raise CryptoError(
                "Failed to decrypt. Master key may have changed or data is corrupted."
            ) from e

    def _load_or_create_key(self) -> bytes:
        if self._key_path.exists():
            return self._key_path.read_bytes().strip()

        self._key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        self._key_path.write_bytes(key)
        self._secure_file(self._key_path)
        return key

    @staticmethod
    def _secure_file(path: Path) -> None:
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
