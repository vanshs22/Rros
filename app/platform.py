"""Small, dependency-free platform services shared by API and workers.

These services keep cross-cutting production concerns out of route handlers:
structured error codes, bounded in-process metrics, feature flags, global search,
and privacy export/anonymisation. Redis/Prometheus integrations can replace the
in-process implementations without changing the API contracts.
"""
from __future__ import annotations

import os
import threading
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .models import (
    AgentDefinition,
    Appointment,
    AuditLog,
    CommunicationEvent,
    Customer,
    IntegrationEvent,
    Lead,
    Review,
    RevenueOpportunity,
    Transaction,
)


ERROR_CODES = {
    "VALIDATION_ERROR", "AUTH_ERROR", "PERMISSION_ERROR", "NOT_FOUND", "CONFLICT",
    "RATE_LIMITED", "PROVIDER_ERROR", "TEMPORARY_ERROR", "INTERNAL_ERROR",
    "AMBIGUOUS_EXTERNAL_RESULT",
}

PLAN_CATALOG = {
    "free_trial": {"monthly_opportunity_limit": 100, "max_users": 2, "features": ["dashboard", "simulation"]},
    "starter": {"monthly_opportunity_limit": 1000, "max_users": 5, "features": ["dashboard", "connectors", "simulation"]},
    "growth": {"monthly_opportunity_limit": 10000, "max_users": 25, "features": ["agents", "automation", "analytics"]},
    "agency": {"monthly_opportunity_limit": 100000, "max_users": 100, "features": ["multi_location", "advanced_analytics", "priority_support"]},
    "enterprise": {"monthly_opportunity_limit": 1000000, "max_users": 1000, "features": ["sso", "scim", "custom_retention", "priority_support"]},
}


class PlatformError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        if code not in ERROR_CODES:
            code = "INTERNAL_ERROR"
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class Metrics:
    """Bounded process metrics suitable for health/debug endpoints."""
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts = Counter()
        self._latency_ms = Counter()

    def observe(self, name: str, duration_ms: Optional[float] = None) -> None:
        with self._lock:
            self._counts[name] += 1
            if duration_ms is not None:
                self._latency_ms[name] += int(max(0, duration_ms))

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "counts": dict(self._counts),
                "latency_ms_total": dict(self._latency_ms),
            }


metrics = Metrics()


def feature_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv("RROS_FEATURE_" + name.upper(), "")
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def global_search(db: Session, organization_id: str, query: str, limit: int = 25) -> List[Dict[str, Any]]:
    """Search every user-visible entity while applying the tenant predicate to each query."""
    term = "%" + query.strip()[:120] + "%"
    if len(query.strip()) < 2:
        return []
    per_type = max(1, min(10, limit // 8 + 1))
    results: List[Dict[str, Any]] = []

    for row in db.scalars(select(Customer).where(Customer.organization_id == organization_id, or_(Customer.name.ilike(term), Customer.email.ilike(term), Customer.phone.ilike(term))).limit(per_type)).all():
        results.append({"type": "customer", "id": row.id, "label": row.name, "detail": row.email or row.phone})
    for row in db.scalars(select(Lead).where(Lead.organization_id == organization_id, or_(Lead.external_id.ilike(term), Lead.source.ilike(term), Lead.status.ilike(term))).limit(per_type)).all():
        results.append({"type": "lead", "id": row.id, "label": row.external_id or row.id, "detail": row.status})
    for row in db.scalars(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == organization_id, or_(RevenueOpportunity.id.ilike(term), RevenueOpportunity.type.ilike(term), RevenueOpportunity.reason.ilike(term))).limit(per_type)).all():
        results.append({"type": "opportunity", "id": row.id, "label": row.type, "detail": row.reason[:160]})
    for row in db.scalars(select(Review).where(Review.organization_id == organization_id, or_(Review.text.ilike(term), Review.source.ilike(term), Review.topic.ilike(term))).limit(per_type)).all():
        results.append({"type": "review", "id": row.id, "label": row.source, "detail": row.text[:160]})
    for row in db.scalars(select(Appointment).where(Appointment.organization_id == organization_id, Appointment.status.ilike(term)).limit(per_type)).all():
        results.append({"type": "appointment", "id": row.id, "label": row.status, "detail": row.scheduled_at.isoformat()})
    for row in db.scalars(select(Transaction).where(Transaction.organization_id == organization_id, or_(Transaction.id.ilike(term), Transaction.external_id.ilike(term), Transaction.status.ilike(term))).limit(per_type)).all():
        results.append({"type": "transaction", "id": row.id, "label": row.status, "detail": str(row.amount)})
    for row in db.scalars(select(AgentDefinition).where(AgentDefinition.organization_id == organization_id, or_(AgentDefinition.key.ilike(term), AgentDefinition.name.ilike(term))).limit(per_type)).all():
        results.append({"type": "agent", "id": row.id, "label": row.name, "detail": row.autonomy_mode})
    for row in db.scalars(select(IntegrationEvent).where(IntegrationEvent.organization_id == organization_id, or_(IntegrationEvent.event_type.ilike(term), IntegrationEvent.external_id.ilike(term))).limit(per_type)).all():
        results.append({"type": "integration_event", "id": str(row.id), "label": row.event_type, "detail": row.external_id})
    return results[: max(1, min(100, limit))]


def customer_export(db: Session, organization_id: str, customer_id: str) -> Dict[str, Any]:
    customer = db.scalar(select(Customer).where(Customer.organization_id == organization_id, Customer.id == customer_id))
    if not customer:
        raise PlatformError("NOT_FOUND", "Customer not found", 404)
    return {
        "customer": {"id": customer.id, "name": customer.name, "email": customer.email, "phone": customer.phone, "lifecycle_status": customer.lifecycle_status},
        "leads": [{"id": x.id, "status": x.status, "source": x.source, "value": str(x.value)} for x in db.scalars(select(Lead).where(Lead.organization_id == organization_id, Lead.customer_id == customer_id)).all()],
        "transactions": [{"id": x.id, "amount": str(x.amount), "status": x.status, "paid_at": x.paid_at.isoformat()} for x in db.scalars(select(Transaction).where(Transaction.organization_id == organization_id, Transaction.customer_id == customer_id)).all()],
        "reviews": [{"id": x.id, "rating": x.rating, "text": x.text, "source": x.source} for x in db.scalars(select(Review).where(Review.organization_id == organization_id, Review.customer_id == customer_id)).all()],
        "communications": [{"id": x.id, "channel": x.channel, "direction": x.direction, "status": x.status, "occurred_at": x.occurred_at.isoformat()} for x in db.scalars(select(CommunicationEvent).where(CommunicationEvent.organization_id == organization_id, CommunicationEvent.customer_id == customer_id)).all()],
    }


def anonymise_customer(db: Session, organization_id: str, customer_id: str) -> Customer:
    customer = db.scalar(select(Customer).where(Customer.organization_id == organization_id, Customer.id == customer_id))
    if not customer:
        raise PlatformError("NOT_FOUND", "Customer not found", 404)
    customer.name = "Deleted Customer"
    customer.email = None
    customer.phone = None
    customer.external_id = None
    customer.lifecycle_status = "deleted"
    return customer


def monotonic_ms(start: float) -> float:
    return (time.monotonic() - start) * 1000
