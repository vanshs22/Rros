"""Runtime OAuth token lifecycle for connector workers.

Tokens are refreshed only when close to expiry or when a provider returned 401.
Refresh is serialized per tenant/provider with a row lock so rotated refresh
credentials cannot race across worker processes.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .integrations import PROVIDERS, provider as get_provider, refresh_token
from .models import Integration
from .secret_store import decrypt_json, encrypt_json


def _oauth_client(provider_key: str) -> tuple[Optional[str], Optional[str]]:
    client_id = os.getenv(f"RROS_{provider_key.upper()}_CLIENT_ID")
    client_secret = os.getenv(f"RROS_{provider_key.upper()}_CLIENT_SECRET")
    if provider_key.startswith("google_"):
        client_id = client_id or os.getenv("RROS_GOOGLE_CLIENT_ID")
        client_secret = client_secret or os.getenv("RROS_GOOGLE_CLIENT_SECRET")
    return client_id, client_secret


def _expires_at(creds: dict[str, Any]) -> datetime | None:
    value = creds.get("expires_at")
    if value is None:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _with_expiry(creds: dict[str, Any], token_payload: dict[str, Any]) -> dict[str, Any]:
    merged = dict(creds)
    merged.update(token_payload)
    expires_in = token_payload.get("expires_in")
    if expires_in:
        try:
            merged["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=int(expires_in))).isoformat()
        except (TypeError, ValueError):
            pass
    merged["refresh_token"] = token_payload.get("refresh_token") or creds.get("refresh_token")
    return merged


def needs_refresh(provider_key: str, creds: dict[str, Any], *, force: bool = False) -> bool:
    if force:
        return True
    try:
        p = PROVIDERS[provider_key]
    except KeyError:
        return False
    if p.auth != "oauth" or not creds.get("refresh_token"):
        return False
    expires = _expires_at(creds)
    if expires is None:
        return False
    return expires <= datetime.now(timezone.utc) + timedelta(seconds=max(30, settings.oauth_refresh_skew_seconds))


async def refresh_integration(db: Session, organization_id: str, integration_id: str, *, force: bool = False) -> dict[str, Any]:
    """Refresh an integration and return the newest credential dictionary."""
    row = db.scalar(select(Integration).where(
        Integration.organization_id == organization_id,
        Integration.id == integration_id,
    ).with_for_update())
    if not row or not row.credentials_ciphertext:
        raise ValueError("Integration is not configured")
    creds = decrypt_json(row.credentials_ciphertext)
    if not needs_refresh(row.provider, creds, force=force):
        return creds
    p = get_provider(row.provider)
    refresh = creds.get("refresh_token")
    client_id, client_secret = _oauth_client(row.provider)
    if p.auth != "oauth" or not refresh or not client_id or not client_secret:
        return creds
    tokens = await refresh_token(p, client_id, client_secret, refresh)
    updated = _with_expiry(creds, tokens)
    row.credentials_ciphertext = encrypt_json(updated)
    row.status = "connected"
    row.last_error = None
    row.health_checked_at = datetime.now(timezone.utc)
    db.commit()
    return updated


async def fresh_credentials(db: Session, organization_id: str, integration: Integration) -> dict[str, Any]:
    creds = decrypt_json(integration.credentials_ciphertext)
    if needs_refresh(integration.provider, creds):
        return await refresh_integration(db, organization_id, integration.id)
    return creds
