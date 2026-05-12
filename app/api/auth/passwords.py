import base64
import hashlib
import hmac
import secrets

from app.config.settings import settings

_HASH_SCHEME = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, settings.PASSWORD_HASH_ITERATIONS)
    salt_b64 = base64.urlsafe_b64encode(salt).decode("ascii")
    key_b64 = base64.urlsafe_b64encode(key).decode("ascii")
    return f"{_HASH_SCHEME}${settings.PASSWORD_HASH_ITERATIONS}${salt_b64}${key_b64}"


def verify_password(password: str, stored_hash: str) -> bool:
    parts = stored_hash.split("$")
    if len(parts) != 4:
        return False
    scheme, iteration_str, salt_b64, expected_key_b64 = parts
    if scheme != _HASH_SCHEME:
        return False
    salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
    expected_key = base64.urlsafe_b64decode(expected_key_b64.encode("ascii"))
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iteration_str))
    return hmac.compare_digest(candidate, expected_key)
