import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import Organization, User

bearer = HTTPBearer(auto_error=False)
ACCESS_COOKIE = "rros_access"
CSRF_COOKIE = "rros_csrf"


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310_000)
    return f"pbkdf2_sha256$310000${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        try:
            legacy = hashlib.pbkdf2_hmac("sha256", password.encode(), b"revenue-recovery-v1", 150_000).hex()
            return hmac.compare_digest(legacy, encoded)
        except Exception:
            return False


def issue_token(user: User) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user.id,
            "org": user.organization_id,
            "role": user.role,
            "tv": user.token_version,
            "iat": now,
            "exp": now + timedelta(minutes=settings.jwt_exp_minutes),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def _token_from_request(request: Request, creds: HTTPAuthorizationCredentials | None) -> Optional[str]:
    if creds:
        return creds.credentials
    return request.cookies.get(ACCESS_COOKIE)


def current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
):
    token = _token_from_request(request, creds)
    if not token:
        raise HTTPException(401, "Authentication required")
    try:
        data = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    org_id = data.get("org")
    if settings.database_url.startswith("postgresql") and org_id:
        db.execute(text("select set_config('app.current_org', :org, true)"), {"org": org_id})
    user = db.get(User, data.get("sub"))
    if not user or not user.active or user.organization_id != org_id or user.token_version != data.get("tv"):
        raise HTTPException(401, "User session is invalid")
    org = db.get(Organization, user.organization_id)
    if not org or not org.active:
        raise HTTPException(403, "Organization is inactive")
    return user


def require_role(*roles):
    def dep(user: User = Depends(current_user)):
        if user.role not in roles:
            raise HTTPException(403, "Insufficient permissions")
        return user
    return dep


def set_auth_cookies(response, token: str):
    # HttpOnly access token prevents ordinary JS from exfiltrating the session.
    secure = settings.public_base_url.lower().startswith("https://") or settings.app_env == "production"
    response.set_cookie(ACCESS_COOKIE, token, httponly=True, secure=secure, samesite="lax", max_age=settings.jwt_exp_minutes * 60, path="/")
    response.set_cookie(CSRF_COOKIE, secrets.token_urlsafe(24), httponly=False, secure=secure, samesite="lax", max_age=settings.jwt_exp_minutes * 60, path="/")


def clear_auth_cookies(response):
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
