from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

AUTO_ID = BigInteger().with_variant(Integer(), "sqlite")

from .db import Base


def utcnow():
    return datetime.now(timezone.utc)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, index=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    plan_code: Mapped[str] = mapped_column(String(40), default="starter", nullable=False)
    subscription_status: Mapped[str] = mapped_column(String(40), default="trialing", nullable=False)
    monthly_opportunity_limit: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    brand_name: Mapped[Optional[str]] = mapped_column(String(200))
    brand_logo_url: Mapped[Optional[str]] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    webhook_secret_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    webhook_secret_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (UniqueConstraint("id", name="uq_org_id_ref"),)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(320), index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(40), default="member", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    token_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_user_org_email"),
        UniqueConstraint("organization_id", "id", name="uq_user_org_id_ref"),
        Index("ix_user_email_lower", "email"),
        Index("ux_user_org_email_ci", "organization_id", text("lower(email)"), unique=True),
        CheckConstraint("role IN ('owner','admin','manager','agent','analyst','member','viewer')", name="ck_user_role"),
    )


class Location(Base):
    __tablename__ = "locations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_location_org_name"),
        UniqueConstraint("organization_id", "id", name="uq_location_org_id_ref"),
    )


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    location_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(320), index=True)
    phone: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    lifecycle_status: Mapped[str] = mapped_column(String(40), default="active", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_customer_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_customer_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            name="fk_customer_org_location",
        ),
    )


class CustomerIdentity(Base):
    __tablename__ = "customer_identities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="connector", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "external_id", name="uq_customer_identity_org_provider_external"),
        ForeignKeyConstraint(["organization_id", "customer_id"], ["customers.organization_id", "customers.id"], name="fk_customer_identity_org_customer"),
    )


class Lead(Base):
    __tablename__ = "leads"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    source: Mapped[str] = mapped_column(String(100), default="unknown", nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="new", index=True, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_contact_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    quote_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_lead_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_lead_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_lead_org_customer",
        ),
        CheckConstraint("value >= 0", name="ck_lead_value_nonnegative"),
    )


class Appointment(Base):
    __tablename__ = "appointments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    lead_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"), nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_appointment_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_appointment_org_customer",
        ),
        ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            name="fk_appointment_org_lead",
        ),
        CheckConstraint("value >= 0", name="ck_appointment_value_nonnegative"),
    )


class ServiceJob(Base):
    """Canonical field-service/work-order record kept separate from appointments.

    A provider job can be scheduled, completed, invoiced, or purely operational;
    it must never be silently coerced into an appointment because the data grain is different.
    """
    __tablename__ = "service_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[Optional[str]] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(60), index=True, nullable=False, default="open")
    value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"), nullable=False)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_service_job_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_service_job_org_id_ref"),
        ForeignKeyConstraint(["organization_id", "customer_id"], ["customers.organization_id", "customers.id"], name="fk_service_job_org_customer"),
        CheckConstraint("value >= 0", name="ck_service_job_value_nonnegative"),
    )


class Transaction(Base):
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    source_opportunity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_tx_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_tx_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_tx_org_customer",
        ),
        ForeignKeyConstraint(
            ["organization_id", "source_opportunity_id"],
            ["revenue_opportunities.organization_id", "revenue_opportunities.id"],
            name="fk_tx_org_source_opportunity",
        ),
        CheckConstraint("amount >= 0", name="ck_tx_amount_nonnegative"),
    )


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    failed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_code: Mapped[Optional[str]] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_payment_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_payment_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_payment_org_customer",
        ),
        CheckConstraint("amount >= 0", name="ck_payment_amount_nonnegative"),
        CheckConstraint("retry_count >= 0", name="ck_payment_retry_count_nonnegative"),
    )


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    amount_due: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_invoice_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_invoice_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_invoice_org_customer",
        ),
        CheckConstraint("total >= 0 AND amount_due >= 0 AND amount_due <= total", name="ck_invoice_amounts"),
    )


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    source: Mapped[str] = mapped_column(String(60), default="unknown", nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    topic: Mapped[Optional[str]] = mapped_column(String(100))
    severity: Mapped[Optional[str]] = mapped_column(String(40))
    source_url: Mapped[Optional[str]] = mapped_column(String(1000))
    analysis_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_review_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_review_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_review_org_customer",
        ),
        CheckConstraint("rating BETWEEN 1 AND 5", name="ck_review_rating"),
    )


class ReviewResponse(Base):
    __tablename__ = "review_responses"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    review_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    approved_by_user_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    external_response_id: Mapped[Optional[str]] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "review_id", name="uq_review_response_org_review"),
        ForeignKeyConstraint(
            ["organization_id", "review_id"],
            ["reviews.organization_id", "reviews.id"],
            name="fk_review_response_org_review",
        ),
        ForeignKeyConstraint(
            ["organization_id", "approved_by_user_id"],
            ["users.organization_id", "users.id"],
            name="fk_review_response_org_approver",
        ),
        CheckConstraint("status IN ('draft','approved','sent','failed','cancelled')", name="ck_review_response_status"),
    )


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(24), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes_json: Mapped[str] = mapped_column(Text, default='["ingest:write"]', nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_api_key_org_name"),
        UniqueConstraint("organization_id", "id", name="uq_api_key_org_id_ref"),
        CheckConstraint("length(key_hash) = 64", name="ck_api_key_hash_length"),
    )


class CommunicationEvent(Base):
    __tablename__ = "communication_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    lead_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    provider: Mapped[str] = mapped_column(String(60), default="generic", nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    subject: Mapped[Optional[str]] = mapped_column(String(500))
    body_preview: Mapped[Optional[str]] = mapped_column(Text)
    thread_key: Mapped[Optional[str]] = mapped_column(String(200), index=True)
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_comm_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_comm_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_comm_org_customer",
        ),
        ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            name="fk_comm_org_lead",
        ),
        CheckConstraint("channel IN ('sms','email','whatsapp','voice')", name="ck_comm_channel"),
        CheckConstraint("direction IN ('inbound','outbound')", name="ck_comm_direction"),
    )


class CallEvent(Base):
    __tablename__ = "call_events"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    lead_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(160), nullable=True)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "external_id", name="uq_call_org_external"),
        UniqueConstraint("organization_id", "id", name="uq_call_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_call_org_customer",
        ),
        ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            name="fk_call_org_lead",
        ),
        CheckConstraint("duration_seconds >= 0", name="ck_call_duration_nonnegative"),
    )


class ChannelConsent(Base):
    __tablename__ = "channel_consents"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    customer_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    allowed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[str] = mapped_column(String(100), default="unknown", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "customer_id", "channel", name="uq_consent"),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_consent_org_customer",
        ),
        CheckConstraint("channel IN ('sms','email','whatsapp','voice')", name="ck_consent_channel"),
    )


class RevenueOpportunity(Base):
    __tablename__ = "revenue_opportunities"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    location_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    customer_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    lead_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    assigned_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    detection_key: Mapped[str] = mapped_column(String(220), nullable=False)
    type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="open", index=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    estimated_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    probability: Mapped[float] = mapped_column(Numeric(7, 5), nullable=False)
    expected_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(7, 5), nullable=False)
    urgency: Mapped[float] = mapped_column(Numeric(7, 5), default=0.5, nullable=False)
    recoverability: Mapped[float] = mapped_column(Numeric(7, 5), default=0.5, nullable=False)
    customer_importance: Mapped[float] = mapped_column(Numeric(7, 5), default=0.5, nullable=False)
    priority_score: Mapped[float] = mapped_column(Numeric(18, 8), default=0, nullable=False, index=True)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    recovered_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=Decimal("0"), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    recommended_action: Mapped[Optional[str]] = mapped_column(Text)
    attribution_status: Mapped[str] = mapped_column(String(30), default="estimated", nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "detection_key", name="uq_opp_org_detection_key"),
        UniqueConstraint("organization_id", "id", name="uq_opp_org_id_ref"),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            name="fk_opp_org_location",
        ),
        ForeignKeyConstraint(
            ["organization_id", "customer_id"],
            ["customers.organization_id", "customers.id"],
            name="fk_opp_org_customer",
        ),
        ForeignKeyConstraint(
            ["organization_id", "lead_id"],
            ["leads.organization_id", "leads.id"],
            name="fk_opp_org_lead",
        ),
        ForeignKeyConstraint(
            ["organization_id", "assigned_user_id"],
            ["users.organization_id", "users.id"],
            name="fk_opp_org_assigned_user",
        ),
        Index("ix_opp_org_status_value", "organization_id", "status", "expected_value"),
        CheckConstraint("estimated_value >= 0 AND expected_value >= 0 AND recovered_amount >= 0", name="ck_opp_amounts"),
        CheckConstraint("probability >= 0 AND probability <= 1", name="ck_opp_probability"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_opp_confidence"),
        CheckConstraint("urgency >= 0 AND urgency <= 1 AND recoverability >= 0 AND recoverability <= 1 AND customer_importance >= 0 AND customer_importance <= 1", name="ck_opp_priority_inputs"),
        CheckConstraint("severity IN ('low','medium','high','critical')", name="ck_opp_severity"),
        CheckConstraint("status IN ('open','investigating','planned','awaiting_approval','in_recovery','engaged','converted','recovered','lost','dismissed','expired')", name="ck_opp_status"),
    )


class OpportunityEvent(Base):
    __tablename__ = "opportunity_events"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    opportunity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "opportunity_id"],
            ["revenue_opportunities.organization_id", "revenue_opportunities.id"],
            name="fk_opp_event_org_opportunity",
        ),
        Index("ix_opp_events_opp_time", "opportunity_id", "created_at"),
    )


class RecoveryPlan(Base):
    __tablename__ = "recovery_plans"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    opportunity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="ready", nullable=False)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        Index("ix_plan_org_opp", "organization_id", "opportunity_id"),
        Index(
            "ux_plan_active_org_opp",
            "organization_id",
            "opportunity_id",
            unique=True,
            sqlite_where=text("status IN ('awaiting_approval','approved','active')"),
            postgresql_where=text("status IN ('awaiting_approval','approved','active')"),
        ),
        ForeignKeyConstraint(
            ["organization_id", "opportunity_id"],
            ["revenue_opportunities.organization_id", "revenue_opportunities.id"],
            name="fk_plan_org_opportunity",
        ),
        CheckConstraint("channel IN ('sms','email','whatsapp','voice')", name="ck_plan_channel"),
    )


class RecoveryPlaybook(Base):
    __tablename__ = "recovery_playbooks"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    trigger_type: Mapped[str] = mapped_column(String(80), nullable=False)
    conditions_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    steps_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="draft", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_playbook_org_name"),
        UniqueConstraint("organization_id", "id", name="uq_playbook_org_id_ref"),
        CheckConstraint("status IN ('draft','published','paused')", name="ck_playbook_status"),
    )


class AgentDefinition(Base):
    __tablename__ = "agent_definitions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    autonomy_mode: Mapped[str] = mapped_column(String(30), default="recommend", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (UniqueConstraint("organization_id", "key", name="uq_agent_def_org_key"), UniqueConstraint("organization_id", "id", name="uq_agent_def_org_id_ref"), CheckConstraint("status IN ('active','paused','disabled')", name="ck_agent_def_status"), CheckConstraint("autonomy_mode IN ('observe','recommend','draft','approval_required','auto_execute')", name="ck_agent_def_autonomy_mode"))


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    opportunity_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    parent_run_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    agent_definition_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued", index=True, nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(40), default="1.0", nullable=False)
    requested_by: Mapped[Optional[str]] = mapped_column(String(64))
    input_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    output_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_agent_runs_org_created", "organization_id", "created_at"), UniqueConstraint("organization_id", "id", name="uq_agent_run_org_id_ref"), ForeignKeyConstraint(["organization_id","opportunity_id"],["revenue_opportunities.organization_id","revenue_opportunities.id"], name="fk_agent_run_org_opp"), ForeignKeyConstraint(["organization_id","agent_definition_id"],["agent_definitions.organization_id","agent_definitions.id"], name="fk_agent_run_org_def"), CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')", name="ck_agent_run_status"))


class AgentHandoff(Base):
    __tablename__ = "agent_handoffs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    pipeline_run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    from_agent_key: Mapped[str] = mapped_column(String(80), nullable=False)
    to_agent_key: Mapped[str] = mapped_column(String(80), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (ForeignKeyConstraint(["organization_id","pipeline_run_id"],["agent_runs.organization_id","agent_runs.id"], name="fk_handoff_org_run"), Index("ix_handoff_org_run", "organization_id", "pipeline_run_id"))


class AgentMemory(Base):
    __tablename__ = "agent_memory"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    scope_type: Mapped[str] = mapped_column(String(30), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(160), index=True, nullable=False)
    memory_type: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    content_json: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(7,5), default=0, nullable=False)
    source_run_id: Mapped[Optional[str]] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (Index("ix_agent_memory_scope", "organization_id", "scope_type", "scope_id"), CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_agent_memory_confidence"))


class AgentOutcome(Base):
    __tablename__ = "agent_outcomes"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    opportunity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    outcome: Mapped[str] = mapped_column(String(50), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18,2), default=Decimal("0"), nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (ForeignKeyConstraint(["organization_id","opportunity_id"],["revenue_opportunities.organization_id","revenue_opportunities.id"], name="fk_agent_outcome_org_opp"), Index("ix_agent_outcome_org_created", "organization_id", "created_at"))


class AgentFeedback(Base):
    __tablename__ = "agent_feedback"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    agent_run_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    agent_key: Mapped[str] = mapped_column(String(80), nullable=False)
    rating: Mapped[int] = mapped_column(Integer, nullable=False)
    correction: Mapped[Optional[str]] = mapped_column(Text)
    created_by_user_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (ForeignKeyConstraint(["organization_id","agent_run_id"],["agent_runs.organization_id","agent_runs.id"], name="fk_feedback_org_run"), ForeignKeyConstraint(["organization_id","created_by_user_id"],["users.organization_id","users.id"], name="fk_feedback_org_user"), CheckConstraint("rating BETWEEN 1 AND 5", name="ck_agent_feedback_rating"))


class RecoveryAction(Base):
    __tablename__ = "recovery_actions"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    opportunity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    action_type: Mapped[str] = mapped_column(String(60), nullable=False)
    channel: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending", index=True, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_action_org_idempotency"),
        Index("ix_action_org_status", "organization_id", "status"),
        ForeignKeyConstraint(
            ["organization_id", "opportunity_id"],
            ["revenue_opportunities.organization_id", "revenue_opportunities.id"],
            name="fk_action_org_opportunity",
        ),
        CheckConstraint("channel IN ('sms','email','whatsapp','voice')", name="ck_action_channel"),
        CheckConstraint("status IN ('pending','approved','queued','sending','sent','delivered','simulated','failed','cancelled')", name="ck_action_status"),
    )


class RecoveryApproval(Base):
    __tablename__ = "recovery_approvals"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    action_id: Mapped[int] = mapped_column(AUTO_ID, nullable=False, index=True)
    approved_by_user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[Optional[str]] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(String(40), default="1", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "action_id", name="uq_approval_org_action"),
        ForeignKeyConstraint(
            ["organization_id", "action_id"],
            ["recovery_actions.organization_id", "recovery_actions.id"],
            name="fk_approval_org_action",
        ),
        ForeignKeyConstraint(
            ["organization_id", "approved_by_user_id"],
            ["users.organization_id", "users.id"],
            name="fk_approval_org_user",
        ),
        CheckConstraint("decision IN ('approved','rejected')", name="ck_approval_decision"),
    )


class AttributionLink(Base):
    __tablename__ = "attribution_links"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    opportunity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    classification: Mapped[str] = mapped_column(String(30), nullable=False)
    attributed_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(7, 5), nullable=False)
    primary_attribution: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "transaction_id"],
            ["transactions.organization_id", "transactions.id"],
            name="fk_attr_org_transaction",
        ),
        ForeignKeyConstraint(
            ["organization_id", "opportunity_id"],
            ["revenue_opportunities.organization_id", "revenue_opportunities.id"],
            name="fk_attr_org_opportunity",
        ),
        UniqueConstraint("organization_id", "transaction_id", "opportunity_id", name="uq_attr_transaction_opportunity"),
        UniqueConstraint("organization_id", "transaction_id", "opportunity_id", "classification", name="uq_attr_link"),
        CheckConstraint("classification IN ('direct','assisted','influenced','estimated','unattributed')", name="ck_attr_classification"),
        CheckConstraint("attributed_amount >= 0", name="ck_attr_amount"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_attr_confidence"),
        Index("ux_attr_primary_transaction", "organization_id", "transaction_id", unique=True, sqlite_where=text("primary_attribution = 1"), postgresql_where=text("primary_attribution = true")),
    )


class AttributionEvent(Base):
    __tablename__ = "attribution_events"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    opportunity_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "opportunity_id"],
            ["revenue_opportunities.organization_id", "revenue_opportunities.id"],
            name="fk_attr_event_org_opportunity",
        ),
    )


class MarketingSpend(Base):
    __tablename__ = "marketing_spend"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    location_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    period_start: Mapped["date"] = mapped_column(Date, nullable=False)
    period_end: Mapped["date"] = mapped_column(Date, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "location_id", "source", "period_start", "period_end", name="uq_spend_org_period"),
        Index("ux_spend_org_period_effective", "organization_id", text("coalesce(location_id, '')"), "source", "period_start", "period_end", unique=True),
        ForeignKeyConstraint(
            ["organization_id", "location_id"],
            ["locations.organization_id", "locations.id"],
            name="fk_spend_org_location",
        ),
        CheckConstraint("amount >= 0", name="ck_spend_amount_nonnegative"),
        CheckConstraint("period_end >= period_start", name="ck_spend_period"),
        Index("ix_spend_org_source_period", "organization_id", "source", "period_start", "period_end"),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[Optional[str]] = mapped_column(ForeignKey("organizations.id"), nullable=True, index=True)
    actor_user_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[Optional[str]] = mapped_column(String(128))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "actor_user_id"],
            ["users.organization_id", "users.id"],
            name="fk_audit_org_actor_user",
        ),
    )


class Integration(Base):
    __tablename__ = "integrations"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="not_configured", nullable=False)
    credentials_ref: Mapped[Optional[str]] = mapped_column(String(200))
    credentials_ciphertext: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    account_metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    last_sync_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    health_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("organization_id", "provider", name="uq_integration_org_provider"),)


class IntegrationEvent(Base):
    __tablename__ = "integration_events"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    integration_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", "external_id", name="uq_integration_event_external"),
        ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_int_event_org_integration",
        ),
    )


class ConnectorAction(Base):
    __tablename__ = "connector_actions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    integration_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    opportunity_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(255))
    request_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    response_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    result_uncertain: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    approved_by_user_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    approval_source: Mapped[Optional[str]] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "idempotency_key", name="uq_connector_action_org_key"),
        UniqueConstraint("organization_id", "id", name="uq_connector_action_org_id_ref"),
        ForeignKeyConstraint(["organization_id", "integration_id"], ["integrations.organization_id", "integrations.id"], name="fk_connector_action_org_integration"),
        ForeignKeyConstraint(["organization_id", "opportunity_id"], ["revenue_opportunities.organization_id", "revenue_opportunities.id"], name="fk_connector_action_org_opp"),
        ForeignKeyConstraint(["organization_id", "approved_by_user_id"], ["users.organization_id", "users.id"], name="fk_connector_action_org_approver"),
        CheckConstraint("attempts >= 0", name="ck_connector_action_attempts_nonnegative"),
        CheckConstraint("status IN ('queued','running','retrying','succeeded','failed','needs_reconciliation','cancelled')", name="ck_connector_action_status"),
    )


class IntegrationSyncState(Base):
    __tablename__ = "integration_sync_states"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    integration_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    resource: Mapped[str] = mapped_column(String(100), nullable=False)
    cursor: Mapped[Optional[str]] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(30), default="idle", nullable=False)
    records_synced: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    records_failed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "integration_id", "resource", name="uq_sync_org_integration_resource"),
        ForeignKeyConstraint(
            ["organization_id", "integration_id"],
            ["integrations.organization_id", "integrations.id"],
            name="fk_sync_org_integration",
        ),
        CheckConstraint("records_synced >= 0", name="ck_sync_records_nonnegative"),
        CheckConstraint("records_failed >= 0", name="ck_sync_records_failed_nonnegative"),
        CheckConstraint("last_error_count >= 0", name="ck_sync_error_count_nonnegative"),
    )




class IntegrationJob(Base):
    __tablename__ = "integration_jobs"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    integration_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    connector_action_id: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    provider: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    job_type: Mapped[str] = mapped_column(String(40), index=True, nullable=False)
    resource: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(300), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(30), index=True, nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True, default=utcnow, nullable=False)
    locked_by: Mapped[Optional[str]] = mapped_column(String(120), index=True)
    locked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    last_heartbeat_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    result_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "dedupe_key", name="uq_integration_job_org_dedupe"),
        ForeignKeyConstraint(["organization_id", "integration_id"], ["integrations.organization_id", "integrations.id"], name="fk_job_org_integration"),
        ForeignKeyConstraint(["organization_id", "connector_action_id"], ["connector_actions.organization_id", "connector_actions.id"], name="fk_job_org_action"),
        CheckConstraint("attempts >= 0", name="ck_job_attempts_nonnegative"),
        CheckConstraint("max_attempts BETWEEN 1 AND 100", name="ck_job_max_attempts"),
        CheckConstraint("job_type IN ('connector_sync','connector_action','detect_org','agent_pipeline','integration_event')", name="ck_job_type"),
        CheckConstraint("status IN ('queued','running','retrying','succeeded','failed','dead_letter','cancelled')", name="ck_job_status"),
        Index("ix_integration_job_claim", "status", "available_at", "lease_until", "created_at"),
        Index("uq_active_connector_sync_slot", "organization_id", "integration_id", "resource", unique=True,
              sqlite_where=text("job_type = 'connector_sync' AND status IN ('queued','running','retrying')"),
              postgresql_where=text("job_type = 'connector_sync' AND status IN ('queued','running','retrying')")),
        Index("uq_active_agent_opportunity_slot", "organization_id", "resource", unique=True,
              sqlite_where=text("job_type = 'agent_pipeline' AND status IN ('queued','running','retrying')"),
              postgresql_where=text("job_type = 'agent_pipeline' AND status IN ('queued','running','retrying')")),
    )


class ConnectorRateState(Base):
    __tablename__ = "connector_rate_states"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    tokens_available: Mapped[Decimal] = mapped_column(Numeric(18, 4), default=120, nullable=False)
    last_refill_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    circuit_state: Mapped[str] = mapped_column(String(20), default="closed", nullable=False)
    circuit_open_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[Optional[int]] = mapped_column(Integer)
    last_error: Mapped[Optional[str]] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "provider", name="uq_rate_state_org_provider"),
        CheckConstraint("rate_limit_per_minute > 0", name="ck_rate_limit_positive"),
        CheckConstraint("tokens_available >= 0", name="ck_rate_tokens_nonnegative"),
        CheckConstraint("consecutive_failures >= 0", name="ck_rate_failures_nonnegative"),
        CheckConstraint("circuit_state IN ('closed','open','half_open')", name="ck_circuit_state"),
    )


class ConnectorReceipt(Base):
    __tablename__ = "connector_receipts"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    integration_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    action_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    operation: Mapped[str] = mapped_column(String(120), nullable=False)
    external_id: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_status: Mapped[Optional[str]] = mapped_column(String(60))
    response_hash: Mapped[Optional[str]] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint("organization_id", "action_id", name="uq_receipt_org_action"),
        ForeignKeyConstraint(["organization_id", "integration_id"], ["integrations.organization_id", "integrations.id"], name="fk_receipt_org_integration"),
        ForeignKeyConstraint(["organization_id", "action_id"], ["connector_actions.organization_id", "connector_actions.id"], name="fk_receipt_org_action_ref"),
    )

class IntegrationOAuthState(Base):
    __tablename__ = "integration_oauth_states"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(60), nullable=False)
    state_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["users.organization_id", "users.id"],
            name="fk_oauth_state_org_user",
        ),
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    id: Mapped[int] = mapped_column(AUTO_ID, primary_key=True, autoincrement=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str] = mapped_column(String(120), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="received", nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    error: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("organization_id", "external_id", name="uq_webhook_org_external"),)



class BusinessPolicy(Base):
    """AI Governance Business Policy Model - Point 30 Completion
    
    Defines business policies for AI governance levels:
    - Level 1: Recommend only (human approval required)
    - Level 2: Limited autonomy (value/risk thresholds)
    - Level 3: Full autonomy (with safety controls)
    """
    __tablename__ = "business_policies"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    
    # Policy scope
    policy_type: Mapped[str] = mapped_column(String(80), nullable=False)  # "ai_governance", "compliance", "safety"
    scope_type: Mapped[str] = mapped_column(String(40), nullable=False)   # "organization", "department", "agent_type"
    scope_id: Mapped[Optional[str]] = mapped_column(String(64))           # Department ID or agent definition key
    
    # Governance level (1-3)
    governance_level: Mapped[int] = mapped_column(Integer, nullable=False)
    
    # Policy configuration
    config_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    
    # Enforcement
    severity: Mapped[str] = mapped_column(String(20), default="medium", nullable=False)
    enforcement_mode: Mapped[str] = mapped_column(String(30), default="block", nullable=False)
    
    # Status
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    
    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    
    __table_args__ = (
        UniqueConstraint("organization_id", "name", name="uq_business_policy_org_name"),
        UniqueConstraint("organization_id", "scope_type", "scope_id", "policy_type", name="uq_business_policy_scope"),
        CheckConstraint("governance_level BETWEEN 1 AND 5", name="ck_governance_level"),
        CheckConstraint("severity IN ('low', 'medium', 'high', 'critical')", name="ck_policy_severity"),
        CheckConstraint("enforcement_mode IN ('block', 'warn', 'require_approval', 'log_only')", name="ck_enforcement_mode"),
        CheckConstraint("policy_type IN ('ai_governance', 'compliance', 'safety', 'communication', 'financial')", name="ck_policy_type"),
        CheckConstraint("scope_type IN ('organization', 'department', 'agent_type', 'channel', 'customer_segment')", name="ck_scope_type"),
        Index("ix_business_policy_org_active", "organization_id", "active", "effective_from", "expires_at"),
    )


class BillingSession(Base):
    """Tenant-scoped Stripe Checkout session and webhook reconciliation record."""
    __tablename__ = "billing_sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("organizations.id"), index=True, nullable=False)
    provider_session_id: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    plan_code: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="created", nullable=False)
    checkout_url: Mapped[Optional[str]] = mapped_column(String(2000))
    provider_customer_id: Mapped[Optional[str]] = mapped_column(String(255))
    provider_subscription_id: Mapped[Optional[str]] = mapped_column(String(255))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("organization_id", "provider_session_id", name="uq_billing_session_org_provider"),
        CheckConstraint("status IN ('created','completed','expired','cancelled','failed')", name="ck_billing_session_status"),
    )
