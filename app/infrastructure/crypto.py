from cryptography.fernet import Fernet

from app.config.settings import settings

_KEY: str | None = None


def _get_fernet() -> Fernet:
    global _KEY
    if _KEY is None:
        _KEY = settings.ENCRYPTION_KEY
    return Fernet(_KEY.encode() if isinstance(_KEY, str) else _KEY)


def encrypt_dsn(dsn: str) -> str:
    return _get_fernet().encrypt(dsn.encode()).decode()


def decrypt_dsn(encrypted: str) -> str:
    return _get_fernet().decrypt(encrypted.encode()).decode()


def encrypt_secret(value: str) -> str:
    return encrypt_dsn(value)


def decrypt_secret(encrypted: str) -> str:
    return decrypt_dsn(encrypted)
