import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from .config import settings


def _fernet():
    if settings.integration_encryption_key:
        raw = settings.integration_encryption_key.encode()
    elif settings.app_env in {"development", "test"}:
        raw = settings.jwt_secret.encode()
    else:
        raise RuntimeError("INTEGRATION_ENCRYPTION_KEY must be configured in production")
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def encrypt_json(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode()).decode()


def decrypt_json(value: str) -> dict:
    try:
        return json.loads(_fernet().decrypt(value.encode()).decode())
    except InvalidToken as exc:
        raise ValueError("Integration credential cannot be decrypted with the current encryption key") from exc
