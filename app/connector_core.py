"""Shared connector transport primitives.

Kept separate from provider registries to avoid circular imports between the
core adapters and the extended connector fabric.
"""
from __future__ import annotations

import base64
import asyncio
from dataclasses import dataclass
from typing import Optional, Any

import httpx


class ConnectorError(RuntimeError):
    """Provider failure with explicit retry semantics.

    retryable means the provider operation is safe to repeat. ambiguous means a
    non-idempotent outbound operation may have reached the provider even though no
    response was received; such operations require reconciliation rather than a
    blind retry.
    """
    def __init__(self, message: str, *, retryable: bool = False, ambiguous: bool = False, status_code: Optional[int] = None):
        super().__init__(message)
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.status_code = status_code


@dataclass
class ConnectorResult:
    provider: str
    operation: str
    records: list[dict[str, Any]]
    next_cursor: Optional[str] = None
    external_id: Optional[str] = None
    raw: dict[str, Any] | None = None


class BaseAdapter:
    provider = ""

    def __init__(self, credentials: dict[str, Any], config: dict[str, Any] | None = None):
        self.credentials = credentials or {}
        self.config = config or {}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=8), follow_redirects=False)
        self.last_response_headers: dict[str, str] = {}

    async def close(self):
        await self.client.aclose()

    def token(self):
        return self.credentials.get("access_token") or self.credentials.get("api_key") or self.credentials.get("token") or self.credentials.get("auth_token")

    async def execute(self, operation: str, args: dict[str, Any], *, idempotency_key: Optional[str] = None) -> ConnectorResult:
        """Common action dispatch for mature adapters; fabric adapters override this."""
        if operation in {"send.sms", "send.whatsapp"} and hasattr(self, "send_message"):
            return await self.send_message(args["to"], args["body"], from_=args.get("from"), whatsapp=operation == "send.whatsapp", status_callback=args.get("status_callback"))
        if operation == "send.email" and hasattr(self, "send_email"):
            return await self.send_email(args["to"], args.get("subject", "Following up"), args["body"])
        if operation == "create_event" and hasattr(self, "create_event"):
            return await self.create_event(args["summary"], args["start"], args["end"], args.get("attendee_email"))
        if operation == "upsert_contact" and hasattr(self, "upsert_contact"):
            return await self.upsert_contact(args["email"], args.get("firstname"), args.get("lastname"), args.get("phone"))
        if operation == "reply_review" and hasattr(self, "reply_review"):
            return await self.reply_review(args["review_name"], args["comment"])
        raise ConnectorError(f"{self.provider} live operation {operation} is not implemented")

    async def request(self, method: str, url: str, *, headers=None, params=None, json_body=None, data=None, retries=3, safe_to_retry: bool = False):
        hdr = {"Accept": "application/json", **(headers or {})}
        method_upper = method.upper()
        can_retry = safe_to_retry or method_upper in {"GET", "HEAD", "OPTIONS"}
        attempts = max(1, int(retries if can_retry else 1))
        last = None
        for attempt in range(attempts):
            try:
                r = await self.client.request(method, url, headers=hdr, params=params, json=json_body, data=data)
                self.last_response_headers = {k.lower(): v for k, v in r.headers.items()}
                if 300 <= r.status_code < 400:
                    raise ConnectorError(f"{self.provider} returned unexpected HTTP redirect {r.status_code}", retryable=False, status_code=r.status_code)
                if r.status_code == 429 or r.status_code >= 500:
                    last = r
                    # Rate limiting (429) is safe to retry because the provider did not
                    # accept the request as a completed action. For 5xx, only retry
                    # automatically when the operation is explicitly safe/idempotent.
                    retry_allowed = (r.status_code == 429) or can_retry
                    if retry_allowed and attempt + 1 < attempts:
                        retry_after = r.headers.get("Retry-After")
                        try:
                            delay = min(60.0, max(0.25, float(retry_after))) if retry_after else min(8.0, 2 ** attempt)
                        except ValueError:
                            delay = min(8.0, 2 ** attempt)
                        await __import__("asyncio").sleep(delay)
                        continue
                    raise ConnectorError(
                        f"{self.provider} returned HTTP {r.status_code}",
                        retryable=(r.status_code == 429) or can_retry,
                        ambiguous=(method_upper not in {"GET", "HEAD", "OPTIONS"}) and r.status_code != 429,
                        status_code=r.status_code,
                    )
                if r.status_code >= 400:
                    raise ConnectorError(f"{self.provider} returned HTTP {r.status_code}", retryable=False, status_code=r.status_code)
                if not r.content:
                    return {}
                try:
                    return r.json()
                except ValueError:
                    return {"text": r.text}
            except httpx.HTTPError as exc:
                if attempt + 1 < attempts:
                    await __import__("asyncio").sleep(min(8.0, 2 ** attempt))
                    continue
                raise ConnectorError(f"{self.provider} network error; provider result may be unknown" if method_upper not in {"GET", "HEAD", "OPTIONS"} else f"{self.provider} network error", retryable=can_retry, ambiguous=(method_upper not in {"GET", "HEAD", "OPTIONS"})) from exc
        raise ConnectorError(f"{self.provider} unavailable ({last.status_code if last else 'network'})", retryable=can_retry)

