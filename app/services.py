import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import settings
from .models import *

OPEN = {"open", "investigating", "planned", "awaiting_approval", "in_recovery", "engaged"}
TERMINAL = {"recovered", "lost", "dismissed", "expired"}


def jdump(x):
    return json.dumps(x, separators=(",", ":"), default=str)


def aware(dt):
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def money(v) -> Decimal:
    return Decimal(str(v or 0)).quantize(Decimal("0.01"))


def score_priority(expected, urgency, recoverability, customer_importance):
    return round(float(expected) * float(urgency) * float(recoverability) * float(customer_importance), 8)


def add_event(db, org, opp_id, typ, actor, meta=None):
    db.add(
        OpportunityEvent(
            organization_id=org,
            opportunity_id=opp_id,
            event_type=typ,
            actor=actor,
            metadata_json=jdump(meta or {}),
        )
    )


def add_audit(db, org, action, rtype, rid, meta=None, actor_user_id=None):
    db.add(
        AuditLog(
            organization_id=org,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=rtype,
            resource_id=rid,
            metadata_json=jdump(meta or {}),
        )
    )


def transition(db, opp, new_status, actor, meta=None):
    allowed = {
        "open": {"investigating", "planned", "dismissed", "expired"},
        "investigating": {"planned", "open", "dismissed"},
        "planned": {"awaiting_approval", "in_recovery", "dismissed"},
        "awaiting_approval": {"in_recovery", "planned", "dismissed"},
        "in_recovery": {"engaged", "converted", "lost", "expired"},
        "engaged": {"converted", "in_recovery", "lost"},
        "converted": {"recovered", "lost"},
        "recovered": set(),
        "lost": set(),
        "dismissed": set(),
        "expired": set(),
    }
    if new_status not in allowed.get(opp.status, set()):
        raise ValueError(f"invalid transition {opp.status}->{new_status}")
    old = opp.status
    opp.status = new_status
    opp.version += 1
    add_event(db, opp.organization_id, opp.id, "status_changed", actor, {"from": old, "to": new_status, **(meta or {})})


def _safe_insert_or_get(db, model, obj, org, external_field="external_id", external_value=None):
    """Insert an org-scoped external entity without leaking a concurrency race.

    The unique constraint is the final arbiter. A nested transaction lets us recover
    cleanly when two workers race to insert the same provider object.
    """
    try:
        with db.begin_nested():
            db.add(obj)
            db.flush()
        return obj, True
    except IntegrityError:
        if external_value is None:
            raise
        existing = db.scalar(
            select(model).where(
                model.organization_id == org,
                getattr(model, external_field) == str(external_value),
            )
        )
        if existing is None:
            raise
        return existing, False


def upsert_opp(
    db,
    org,
    typ,
    customer_id,
    lead_id,
    location_id,
    est,
    prob,
    conf,
    reason,
    severity,
    action,
    key,
    urgency=0.7,
    recoverability=0.7,
    customer_importance=0.5,
    evidence=None,
):
    est = money(est)
    prob = max(0.0, min(1.0, float(prob)))
    conf = max(0.0, min(1.0, float(conf)))
    urgency = max(0.0, min(1.0, float(urgency)))
    recoverability = max(0.0, min(1.0, float(recoverability)))
    customer_importance = max(0.0, min(1.0, float(customer_importance)))
    expected = money(est * Decimal(str(prob)))
    priority = score_priority(expected, urgency, recoverability, customer_importance)
    evidence = evidence or []

    opp = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == org, RevenueOpportunity.detection_key == key))
    if opp and opp.status in OPEN:
        opp.estimated_value = est
        opp.probability = prob
        opp.expected_value = expected
        opp.confidence = conf
        opp.reason = reason
        opp.recommended_action = action
        opp.detected_at = datetime.now(timezone.utc)
        opp.urgency = urgency
        opp.recoverability = recoverability
        opp.customer_importance = customer_importance
        opp.priority_score = priority
        opp.evidence_json = jdump(evidence)
        opp.version += 1
        add_event(db, org, opp.id, "opportunity_refreshed", "detector", {"expected_value": float(expected), "priority_score": priority, "detection_key": key})
        return opp, False
    if opp:
        return opp, False

    opp = RevenueOpportunity(
        id="opp_" + uuid.uuid4().hex,
        organization_id=org,
        location_id=location_id,
        customer_id=customer_id,
        lead_id=lead_id,
        detection_key=key,
        type=typ,
        status="open",
        reason=reason,
        severity=severity,
        estimated_value=est,
        probability=prob,
        expected_value=expected,
        confidence=conf,
        urgency=urgency,
        recoverability=recoverability,
        customer_importance=customer_importance,
        priority_score=priority,
        evidence_json=jdump(evidence),
        recovered_amount=money(0),
        recommended_action=action,
        attribution_status="estimated",
    )
    try:
        with db.begin_nested():
            db.add(opp)
            db.flush()
    except IntegrityError:
        # Another worker may have created the same tenant-scoped detection key.
        existing = db.scalar(
            select(RevenueOpportunity).where(
                RevenueOpportunity.organization_id == org,
                RevenueOpportunity.detection_key == key,
            )
        )
        if existing is not None:
            return existing, False
        raise
    add_event(
        db,
        org,
        opp.id,
        "opportunity_created",
        "detector",
        {"expected_value": float(expected), "priority_score": priority, "detection_key": key, "evidence": evidence},
    )
    return opp, True


def _importance_from_total(total):
    return max(0.35, min(1.0, 0.35 + (float(total or 0) / 20000.0)))


def _customer_importance_map(db, org):
    rows = db.execute(
        select(Transaction.customer_id, func.coalesce(func.sum(Transaction.amount), 0))
        .where(Transaction.organization_id == org, Transaction.status == "paid", Transaction.customer_id.is_not(None))
        .group_by(Transaction.customer_id)
    ).all()
    return {customer_id: _importance_from_total(total) for customer_id, total in rows}


def _customer_importance(importance_map, customer_id):
    if not customer_id:
        return 0.4
    return importance_map.get(customer_id, 0.35)


def _lead_values(db, org):
    rows = db.execute(select(Lead.id, Lead.value).where(Lead.organization_id == org)).all()
    return {lead_id: value for lead_id, value in rows}


def detect_for_org(db, org, max_new=None):
    created = 0
    quota_limited = max_new is not None and max_new <= 0
    if quota_limited:
        return {"created": 0, "refreshed": 0, "expired": 0, "run_started_at": datetime.now(timezone.utc).isoformat(), "quota_limited": True}
    now = datetime.now(timezone.utc)
    run_started_at = now
    importance = _customer_importance_map(db, org)
    lead_values = _lead_values(db, org)

    def maybe_upsert_opp(*args, **kwargs):
        nonlocal created
        if max_new is not None and created >= max_new:
            return None, 0
        result = upsert_opp(*args, **kwargs)
        created += result[1]
        return result

    # Preload outbound timestamps once. This removes one query per inbound message.
    outbound_threads = {}
    outbound_customers = {}
    for thread_key, customer_id, occurred_at in db.execute(
        select(CommunicationEvent.thread_key, CommunicationEvent.customer_id, func.max(CommunicationEvent.occurred_at))
        .where(CommunicationEvent.organization_id == org, CommunicationEvent.direction == "outbound")
        .group_by(CommunicationEvent.thread_key, CommunicationEvent.customer_id)
    ).all():
        if thread_key:
            outbound_threads[thread_key] = aware(occurred_at)
        if customer_id:
            current = outbound_customers.get(customer_id)
            ts = aware(occurred_at)
            if current is None or (ts and ts > current):
                outbound_customers[customer_id] = ts

    lead_stmt = select(Lead).where(Lead.organization_id == org).order_by(Lead.id)
    for r in db.scalars(lead_stmt).yield_per(500):
        cust_importance = _customer_importance(importance, r.customer_id)
        if r.status == "uncontacted":
            age_days = max(0, (now - aware(r.created_at)).days)
            urgency = min(1.0, 0.65 + age_days / 10)
            _, c = maybe_upsert_opp(
                db, org, "lost_lead", r.customer_id, r.id, None, r.value, 0.72, 0.95,
                "Lead arrived with no recorded follow-up.", "high", "Immediate compliant outreach + owner alert",
                f"lead:{r.id}", urgency=urgency, recoverability=0.85, customer_importance=cust_importance,
                evidence=[{"source": "lead", "id": r.id, "field": "last_contact_at", "value": None}],
            )
        elif r.status == "stalled":
            age_days = max(0, (now - aware(r.last_contact_at or r.created_at)).days)
            urgency = min(1.0, 0.6 + age_days / 14)
            _, c = maybe_upsert_opp(
                db, org, "lost_lead", r.customer_id, r.id, None, r.value, 0.68, 0.92,
                "Lead was contacted but has stalled after follow-up.", "high", "Investigate context, then recovery outreach",
                f"lead:{r.id}", urgency=urgency, recoverability=0.8, customer_importance=cust_importance,
                evidence=[{"source": "lead", "id": r.id, "field": "status", "value": r.status}, {"source": "lead", "id": r.id, "field": "last_contact_at", "value": r.last_contact_at}],
            )
        if r.quote_sent_at and r.status not in {"won", "closed_won", "lost", "closed_lost"}:
            quote_age = max(0, (now - aware(r.quote_sent_at)).days)
            if quote_age >= 5:
                _, c = maybe_upsert_opp(
                    db, org, "quote_decay", r.customer_id, r.id, None, r.value, 0.58, 0.9,
                    f"Quote has been outstanding for {quote_age} days without a recorded close.", "medium",
                    "Follow up on the quote with context-aware outreach", f"quote:{r.id}",
                    urgency=min(1.0, 0.6 + quote_age / 20), recoverability=0.72, customer_importance=cust_importance,
                    evidence=[{"source": "lead", "id": r.id, "field": "quote_sent_at", "value": r.quote_sent_at}, {"source": "lead", "id": r.id, "field": "value", "value": str(r.value)}],
                )
    
    inbound_stmt = select(CommunicationEvent).where(
        CommunicationEvent.organization_id == org,
        CommunicationEvent.direction == "inbound",
        CommunicationEvent.status.in_(["received", "delivered"]),
    ).order_by(CommunicationEvent.id)
    for r in db.scalars(inbound_stmt).yield_per(500):
        latest_outbound = outbound_threads.get(r.thread_key) if r.thread_key else outbound_customers.get(r.customer_id)
        if latest_outbound and latest_outbound > aware(r.occurred_at):
            continue
        age_hours = max(0, int((now - aware(r.occurred_at)).total_seconds() / 3600))
        if age_hours < 24:
            continue
        est = money(lead_values.get(r.lead_id) or Decimal("300"))
        _, c = maybe_upsert_opp(
            db, org, "response_gap", r.customer_id, r.lead_id, None, est, 0.66, 0.92,
            f"Inbound {r.channel} message has had no recorded outbound response for {age_hours} hours.", "high",
            "Assign owner and respond promptly through the consented channel", f"communication:{r.id}",
            urgency=min(1.0, 0.78 + age_hours / 72), recoverability=0.82, customer_importance=_customer_importance(importance, r.customer_id),
            evidence=[{"source":"communication","id":r.id,"field":"occurred_at","value":r.occurred_at},{"source":"communication","id":r.id,"field":"channel","value":r.channel}],
        )

    for r in db.scalars(select(Appointment).where(Appointment.organization_id == org, Appointment.status.in_(["cancelled", "no_show"])).order_by(Appointment.id)).yield_per(500):
        _, c = maybe_upsert_opp(
            db, org, "appointment_leak", r.customer_id, r.lead_id, None, r.value, 0.78, 0.95,
            f"Appointment is {r.status} and no successful recovery is recorded.", "high", "Offer reschedule immediately",
            f"appointment:{r.id}", urgency=0.9, recoverability=0.86, customer_importance=_customer_importance(importance, r.customer_id),
            evidence=[{"source": "appointment", "id": r.id, "field": "status", "value": r.status}, {"source": "appointment", "id": r.id, "field": "scheduled_at", "value": r.scheduled_at}],
        )

    for r in db.scalars(select(CallEvent).where(CallEvent.organization_id == org, CallEvent.direction == "inbound", CallEvent.status.in_(["missed", "abandoned"])).order_by(CallEvent.id)).yield_per(500):
        _, c = maybe_upsert_opp(
            db, org, "missed_call", r.customer_id, r.lead_id, None, Decimal("300"), 0.66, 0.9,
            "Inbound call was missed or abandoned without a recorded callback.", "high", "Return the call promptly using a compliant channel",
            f"call:{r.id}", urgency=0.96, recoverability=0.78, customer_importance=_customer_importance(importance, r.customer_id),
            evidence=[{"source": "call", "id": r.id, "field": "status", "value": r.status}, {"source": "call", "id": r.id, "field": "started_at", "value": r.started_at}],
        )

    for r in db.scalars(select(Payment).where(Payment.organization_id == org, Payment.status == "failed").order_by(Payment.id)).yield_per(500):
        _, c = maybe_upsert_opp(
            db, org, "failed_payment", r.customer_id, None, None, r.amount, 0.83, 0.97,
            "Payment failed and revenue remains collectible.", "medium", "Send compliant payment recovery",
            f"payment:{r.id}", urgency=0.88, recoverability=0.9, customer_importance=_customer_importance(importance, r.customer_id),
            evidence=[{"source": "payment", "id": r.id, "field": "status", "value": r.status}, {"source": "payment", "id": r.id, "field": "failure_code", "value": r.failure_code}],
        )

    for r in db.scalars(select(Invoice).where(Invoice.organization_id == org, Invoice.amount_due > 0, Invoice.status.in_(["open", "overdue", "past_due"])).order_by(Invoice.id)).yield_per(500):
        age = max(0, (now - aware(r.due_at)).days)
        if age > 0:
            _, c = maybe_upsert_opp(
                db, org, "overdue_invoice", r.customer_id, None, None, r.amount_due, 0.8, 0.96,
                f"Invoice is {age} days overdue with {r.amount_due} still due.", "high", "Send a payment reminder with the authorized payment path",
                f"invoice:{r.id}", urgency=min(1.0, 0.75 + age / 30), recoverability=0.88, customer_importance=_customer_importance(importance, r.customer_id),
                evidence=[{"source": "invoice", "id": r.id, "field": "due_at", "value": r.due_at}, {"source": "invoice", "id": r.id, "field": "amount_due", "value": str(r.amount_due)}],
            )

    # Process customer transaction history in batches: O(number of customer batches), not O(customers).
    customer_stmt = select(Customer.id).where(Customer.organization_id == org).order_by(Customer.id)
    batch = []
    for customer_id in db.scalars(customer_stmt).yield_per(500):
        batch.append(customer_id)
        if len(batch) < 500:
            continue
        remaining = max(0, max_new - created) if max_new is not None else None
        created += _detect_dormant_batch(db, org, batch, now, importance, remaining)
        if max_new is not None and created >= max_new:
            quota_limited = True
        batch = []
    if batch:
        remaining = max(0, max_new - created) if max_new is not None else None
        created += _detect_dormant_batch(db, org, batch, now, importance, remaining)
        if max_new is not None and created >= max_new:
            quota_limited = True

    for r in db.scalars(select(Review).where(Review.organization_id == org, Review.rating <= 2).order_by(Review.id)).yield_per(500):
        _, c = maybe_upsert_opp(
            db, org, "review_recovery", r.customer_id, None, None, Decimal("1200"), 0.45, 0.82,
            f"Negative {r.source} review indicates {r.topic or 'service'} issue requiring internal resolution.", "medium",
            "Create service-recovery case; human approval before any public response", f"review:{r.id}",
            urgency=0.72, recoverability=0.58, customer_importance=_customer_importance(importance, r.customer_id),
            evidence=[{"source": "review", "id": r.id, "field": "rating", "value": r.rating}, {"source": "review", "id": r.id, "field": "topic", "value": r.topic}],
        )

    # Reconcile detector-created opportunities only when the detector scanned the full source set.
    if max_new is not None and created >= max_new:
        quota_limited = True

    if quota_limited:
        db.commit()
        return {"created": created, "expired": 0, "refreshed": 0, "organization_id": org, "quota_limited": True}

    # Reconcile detector-created opportunities that were not refreshed during this run.
    # This prevents old leaks from inflating the current opportunity pool after the source
    # system is updated (lead won, payment succeeded, appointment recovered, review resolved, etc.).
    db.flush()
    seen_keys = {
        meta.get("detection_key")
        for (meta,) in db.execute(
            select(OpportunityEvent.metadata_json)
            .where(OpportunityEvent.organization_id == org, OpportunityEvent.actor == "detector", OpportunityEvent.created_at >= run_started_at)
        ).all()
        for meta in [json.loads(meta or "{}")]
        if meta.get("detection_key")
    }
    detector_prefixes = ("lead:", "quote:", "communication:", "appointment:", "call:", "payment:", "invoice:", "dormant:", "review:")
    active_rows = db.scalars(
        select(RevenueOpportunity).where(
            RevenueOpportunity.organization_id == org,
            RevenueOpportunity.status.in_({"open", "investigating", "planned", "awaiting_approval", "in_recovery", "engaged"}),
        )
    ).all()
    expired = 0
    for opp in active_rows:
        if opp.detection_key.startswith(detector_prefixes) and opp.detection_key not in seen_keys and opp.detected_at < run_started_at:
            transition(db, opp, "expired", "detector", {"reason": "source_condition_no_longer_detected"})
            expired += 1

    db.commit()
    return {"created": created, "expired": expired, "refreshed": max(0, len(seen_keys) - created), "organization_id": org}


def _detect_dormant_batch(db, org, customer_ids, now, importance, max_new=None):
    if not customer_ids:
        return 0
    from sqlalchemy import and_, or_
    tx = Transaction
    rn = func.row_number().over(partition_by=tx.customer_id, order_by=tx.paid_at.desc()).label("rn")
    rows = db.execute(
        select(tx.customer_id, tx.id, tx.amount, tx.paid_at, rn)
        .where(tx.organization_id == org, tx.customer_id.in_(customer_ids), tx.status == "paid")
        .order_by(tx.customer_id, tx.paid_at.desc())
    ).all()
    grouped = {}
    for customer_id, txid, amount, paid_at, rownum in rows:
        if rownum <= 20:
            grouped.setdefault(customer_id, []).append((txid, amount, paid_at))
    customer_map = {c.id: c for c in db.scalars(select(Customer).where(Customer.organization_id == org, Customer.id.in_(customer_ids))).all()}
    created = 0
    for customer_id, cust_txs in grouped.items():
        txs = cust_txs
        if len(txs) < 2 or customer_id not in customer_map:
            continue
        gaps = [(aware(txs[i][2]) - aware(txs[i + 1][2])).days for i in range(len(txs) - 1) if txs[i][2] and txs[i + 1][2]]
        if not gaps:
            continue
        baseline = max(1, sum(gaps) / len(gaps))
        current = max(0, (now - aware(txs[0][2])).days)
        if current <= max(60, baseline * 1.7):
            continue
        est = max(txs[0][1], sum((t[1] for t in txs[:3]), Decimal("0")) / min(3, len(txs)))
        cust = customer_map[customer_id]
        if max_new is not None and created >= max_new:
            break
        _, c = upsert_opp(
            db, org, "dormant_customer", cust.id, None, None, est, 0.62, 0.90,
            f"Customer normally returns about every {round(baseline)} days; current gap is {current} days.", "high",
            "Personalized reactivation using a preferred compliant channel", f"dormant:{cust.id}",
            urgency=min(1.0, 0.68 + current / 180), recoverability=0.7, customer_importance=_customer_importance(importance, cust.id),
            evidence=[{"source": "transaction", "id": txs[0][0], "field": "paid_at", "value": txs[0][2]}, {"source": "customer", "id": cust.id, "field": "baseline_days", "value": round(baseline)}],
        )
        created += c
    return created


def dashboard(db, org):
    rows = db.scalars(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == org, RevenueOpportunity.status.not_in(["dismissed", "expired"]))).all()
    recovered = db.scalar(select(func.coalesce(func.sum(AttributionLink.attributed_amount), 0)).where(AttributionLink.organization_id == org, AttributionLink.primary_attribution == True)) or 0
    direct = db.scalar(select(func.coalesce(func.sum(AttributionLink.attributed_amount), 0)).where(AttributionLink.organization_id == org, AttributionLink.primary_attribution == True, AttributionLink.classification == "direct")) or 0
    assisted = db.scalar(select(func.coalesce(func.sum(AttributionLink.attributed_amount), 0)).where(AttributionLink.organization_id == org, AttributionLink.primary_attribution == True, AttributionLink.classification == "assisted")) or 0
    influenced = db.scalar(select(func.coalesce(func.sum(AttributionLink.attributed_amount), 0)).where(AttributionLink.organization_id == org, AttributionLink.primary_attribution == True, AttributionLink.classification == "influenced")) or 0
    return {
        "estimated_opportunity": round(float(sum((r.estimated_value for r in rows), Decimal("0"))), 2),
        "expected_value": round(float(sum((r.expected_value for r in rows), Decimal("0"))), 2),
        "in_recovery": round(float(sum((r.expected_value for r in rows if r.status in {"in_recovery", "engaged"}), Decimal("0"))), 2),
        "actual_recovered": round(float(recovered), 2),
        "directly_attributed": round(float(direct), 2),
        "assisted_attributed": round(float(assisted), 2),
        "influenced_attributed": round(float(influenced), 2),
        "opportunity_count": len(rows),
        "urgent": sum(r.severity in {"high", "critical"} for r in rows),
        "leaks": {
            t: round(float(sum((r.expected_value for r in rows if r.type == t), Decimal("0"))), 2)
            for t in ["lost_lead", "quote_decay", "appointment_leak", "missed_call", "failed_payment", "overdue_invoice", "dormant_customer", "review_recovery"]
        },
    }


def attribute_transaction(db, org, tx_id, opportunity_id, classification="direct"):
    tx = db.scalar(select(Transaction).where(Transaction.id == tx_id, Transaction.organization_id == org))
    opp = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.id == opportunity_id, RevenueOpportunity.organization_id == org))
    if classification not in {"direct", "assisted", "influenced", "estimated", "unattributed"}:
        raise ValueError("Unsupported attribution classification")
    if not tx or not opp:
        raise ValueError("Transaction or opportunity not found in tenant")
    if tx.status != "paid":
        raise ValueError("Only successful transactions can be attributed")
    if opp.status not in {"in_recovery", "engaged", "converted", "recovered"} and classification in {"direct", "assisted", "influenced"}:
        raise ValueError("Opportunity is not in a recoverable attribution state")
    if opp.customer_id and tx.customer_id and opp.customer_id != tx.customer_id:
        raise ValueError("Customer mismatch")

    first_action = db.scalar(select(func.min(RecoveryAction.created_at)).where(RecoveryAction.organization_id == org, RecoveryAction.opportunity_id == opp.id, RecoveryAction.status.in_(["approved", "queued", "sending", "sent", "delivered", "simulated"])))
    if first_action and aware(tx.paid_at) < aware(first_action):
        raise ValueError("Transaction predates the recovery action and cannot be directly attributed")
    anchor = aware(first_action or opp.detected_at)
    if anchor and aware(tx.paid_at) > anchor + timedelta(days=settings.attribution_window_days):
        raise ValueError("Transaction is outside the attribution window")

    existing_primary = db.scalar(select(AttributionLink).where(AttributionLink.organization_id == org, AttributionLink.transaction_id == tx.id, AttributionLink.primary_attribution == True))
    if existing_primary and existing_primary.opportunity_id != opp.id:
        raise ValueError("Transaction already has a primary attribution")
    existing = db.scalar(select(AttributionLink).where(AttributionLink.organization_id == org, AttributionLink.transaction_id == tx.id, AttributionLink.opportunity_id == opp.id))
    if existing:
        if existing.classification != classification:
            raise ValueError("Transaction/opportunity already has a different attribution classification")
        return "directly_attributed" if classification == "direct" else classification

    confidence = {"direct": 0.98, "assisted": 0.8, "influenced": 0.6, "estimated": 0.35, "unattributed": 0.0}[classification]
    attributed_amount = money(tx.amount) if classification in {"direct", "assisted", "influenced"} else money(tx.amount * Decimal(str(confidence)))
    primary = classification == "direct"
    if classification == "unattributed":
        attributed_amount = money(0)
    link = AttributionLink(
        organization_id=org,
        transaction_id=tx.id,
        opportunity_id=opp.id,
        classification=classification,
        attributed_amount=attributed_amount,
        confidence=confidence,
        primary_attribution=primary,
        evidence_json=jdump([{"source": "recovery_action", "opportunity_id": opp.id}, {"source": "transaction", "id": tx.id, "amount": str(tx.amount)}]),
    )
    db.add(link)
    if classification in {"direct", "assisted", "influenced"}:
        if tx.source_opportunity_id is None:
            tx.source_opportunity_id = opp.id
        elif tx.source_opportunity_id != opp.id:
            raise ValueError("Transaction primary source is already assigned to another opportunity")
        opp.attribution_status = classification
        opp.recovered_amount = money((opp.recovered_amount or Decimal("0")) + attributed_amount)
        if opp.status != "recovered":
            if opp.status in {"in_recovery", "engaged", "converted"}:
                opp.status = "recovered"
                opp.version += 1
        add_event(db, org, opp.id, "revenue_recovered", "system", {"transaction_id": tx.id, "amount": float(attributed_amount), "classification": classification})
    return "directly_attributed" if classification == "direct" else classification


def canonical_event(db, org, provider, event_type, data, occurred_at=None):
    """Apply a normalized provider event to the canonical revenue model.

    Provider-specific adapters should normalize vendor payloads into this contract before calling this service.
    Unsupported event types remain safely recorded without fabricating domain objects.
    """
    now = aware(occurred_at) or datetime.now(timezone.utc)
    data = dict(data or {})

    # External identifiers are only unique inside a provider namespace. A Stripe
    # customer "123" and a HubSpot contact "123" must never become the same RROS
    # entity. Raw provider IDs remain intact in integration_events for debugging
    # and reconciliation; canonical entities store provider-scoped IDs.
    namespace = str(provider or "unknown").strip().lower()
    namespace_external_ids = namespace not in {"generic_webhook", "api_key", "manual", "seed", "system"}
    def scoped_external(value):
        if value in (None, ""):
            return value
        value = str(value)
        if not namespace_external_ids:
            return value
        prefix = namespace + ":"
        return value if value.startswith(prefix) else prefix + value
    if data.get("external_id"):
        data["external_id"] = scoped_external(data["external_id"])
    if data.get("customer_external_id"):
        data["customer_external_id"] = scoped_external(data["customer_external_id"])

    def find_customer(external_id=None, email=None, phone=None, internal_id=None):
        if internal_id:
            c = db.scalar(select(Customer).where(Customer.organization_id == org, Customer.id == str(internal_id)))
            if c:
                return c
        if external_id:
            identity = db.scalar(select(CustomerIdentity).where(
                CustomerIdentity.organization_id == org, CustomerIdentity.provider == namespace,
                CustomerIdentity.external_id == str(external_id)
            ))
            if identity:
                c = db.scalar(select(Customer).where(Customer.organization_id == org, Customer.id == identity.customer_id))
                if c:
                    return c
            c = db.scalar(select(Customer).where(Customer.organization_id == org, Customer.external_id == str(external_id)))
            if c:
                return c
        if email:
            c = db.scalar(select(Customer).where(Customer.organization_id == org, Customer.email == str(email).strip().lower()))
            if c:
                return c
        if phone:
            c = db.scalar(select(Customer).where(Customer.organization_id == org, Customer.phone == str(phone)))
            if c:
                return c
        return None

    def remember_identity(customer, external_id=None):
        if not customer or not external_id or not namespace_external_ids:
            return
        ext = str(external_id)
        identity = db.scalar(select(CustomerIdentity).where(
            CustomerIdentity.organization_id == org, CustomerIdentity.provider == namespace, CustomerIdentity.external_id == ext
        ))
        if identity:
            if identity.customer_id != customer.id:
                raise ValueError("Provider external customer ID is already linked to another customer")
            identity.last_seen_at = now
            return
        try:
            with db.begin_nested():
                db.add(CustomerIdentity(
                    id="cid_" + uuid.uuid4().hex, organization_id=org, customer_id=customer.id,
                    provider=namespace, external_id=ext, source="connector", first_seen_at=now, last_seen_at=now,
                ))
                db.flush()
        except IntegrityError:
            identity = db.scalar(select(CustomerIdentity).where(
                CustomerIdentity.organization_id == org, CustomerIdentity.provider == namespace, CustomerIdentity.external_id == ext
            ))
            if identity and identity.customer_id != customer.id:
                raise ValueError("Concurrent customer identity conflict")


    if event_type in {"customer.upserted", "customer.created", "customer.updated"}:
        c = find_customer(data.get("external_id"), data.get("email"), data.get("phone"), data.get("customer_id"))
        if not c:
            candidate = Customer(id="cus_" + uuid.uuid4().hex, organization_id=org, external_id=str(data.get("external_id")) if data.get("external_id") else None, name=str(data.get("name") or "Unknown customer"), email=str(data.get("email")).strip().lower() if data.get("email") else None, phone=str(data.get("phone")) if data.get("phone") else None, created_at=now)
            c, _ = _safe_insert_or_get(db, Customer, candidate, org, external_value=data.get("external_id")) if data.get("external_id") else (None, False)
            if c is None:
                db.add(candidate)
                db.flush()
                c = candidate
        else:
            c.name = str(data.get("name") or c.name)
            c.email = str(data.get("email")).strip().lower() if data.get("email") else c.email
            c.phone = str(data.get("phone")) if data.get("phone") else c.phone
        remember_identity(c, data.get("external_id"))
        return {"entity": "customer", "id": c.id, "action": "upserted"}

    if event_type in {"lead.upserted", "lead.created", "lead.updated"}:
        c = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        if not c and (data.get("customer_external_id") or data.get("customer_email") or data.get("customer_phone")):
            candidate = Customer(id="cus_" + uuid.uuid4().hex, organization_id=org, external_id=str(data.get("customer_external_id")) if data.get("customer_external_id") else None, name=str(data.get("customer_name") or "Unknown customer"), email=str(data.get("customer_email")).strip().lower() if data.get("customer_email") else None, phone=str(data.get("customer_phone")) if data.get("customer_phone") else None, created_at=now)
            if data.get("customer_external_id"):
                c, _ = _safe_insert_or_get(db, Customer, candidate, org, external_value=data.get("customer_external_id"))
            else:
                db.add(candidate); db.flush(); c = candidate
        q = select(Lead).where(Lead.organization_id == org, Lead.external_id == str(data.get("external_id"))) if data.get("external_id") else None
        lead = db.scalar(q) if q is not None else None
        if not lead:
            candidate = Lead(id="lead_" + uuid.uuid4().hex, organization_id=org, customer_id=c.id if c else None, external_id=str(data.get("external_id")) if data.get("external_id") else None, source=str(data.get("source") or provider), status=str(data.get("status") or "new"), value=money(data.get("value") or 0), created_at=now)
            if data.get("external_id"):
                lead, _ = _safe_insert_or_get(db, Lead, candidate, org, external_value=data.get("external_id"))
            else:
                db.add(candidate); db.flush(); lead = candidate
        else:
            lead.customer_id = c.id if c else lead.customer_id
            lead.status = str(data.get("status") or lead.status)
            lead.value = money(data.get("value")) if data.get("value") is not None else lead.value
        remember_identity(c, data.get("customer_external_id"))
        return {"entity": "lead", "id": lead.id, "action": "upserted"}

    if event_type in {"communication.received", "message.received", "email.received", "sms.received", "whatsapp.received", "communication.sent", "message.sent", "email.sent", "sms.sent", "whatsapp.sent"}:
        channel = str(data.get("channel") or (event_type.split(".")[0] if event_type.split(".")[0] in {"email", "sms", "whatsapp"} else "email"))
        direction = "inbound" if event_type.endswith("received") else "outbound"
        customer = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        communication = db.scalar(
            select(CommunicationEvent).where(CommunicationEvent.organization_id == org, CommunicationEvent.external_id == str(data.get("external_id")))
        ) if data.get("external_id") else None
        occurred = data.get("occurred_at") or now
        if isinstance(occurred, str):
            occurred = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        if not communication:
            communication = CommunicationEvent(
                id=str(data.get("id") or "msg_" + uuid.uuid4().hex),
                organization_id=org,
                customer_id=customer.id if customer else None,
                lead_id=data.get("lead_id"),
                external_id=str(data.get("external_id")) if data.get("external_id") else None,
                provider=str(data.get("provider") or provider),
                channel=channel, direction=direction, status=str(data.get("status") or ("received" if direction == "inbound" else "sent")),
                occurred_at=aware(occurred), subject=str(data.get("subject") or "")[:500] or None,
                body_preview=str(data.get("body_preview") or data.get("body") or "")[:4000] or None,
                thread_key=str(data.get("thread_key")) if data.get("thread_key") else None,
            )
            if data.get("external_id"):
                communication, _ = _safe_insert_or_get(db, CommunicationEvent, communication, org, external_value=data.get("external_id"))
            else:
                db.add(communication); db.flush()
        else:
            communication.status = str(data.get("status") or communication.status)
            communication.occurred_at = aware(occurred)
            if data.get("body_preview") or data.get("body"):
                communication.body_preview = str(data.get("body_preview") or data.get("body"))[:4000]
        # Keep the lead's operational timestamp synchronized with normalized outbound events.
        if direction == "outbound" and communication.lead_id:
            lead_row = db.scalar(select(Lead).where(Lead.organization_id == org, Lead.id == communication.lead_id))
            if lead_row is not None:
                sent_at = aware(communication.occurred_at)
                current = aware(lead_row.last_contact_at)
                if current is None or (sent_at and sent_at > current):
                    lead_row.last_contact_at = sent_at
        remember_identity(customer, data.get("customer_external_id"))
        return {"entity": "communication", "id": communication.id, "action": "upserted"}

    if event_type in {"service_job.upserted", "service_job.completed", "service_job.cancelled"}:
        customer = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        ext = data.get("external_id")
        service_job = db.scalar(select(ServiceJob).where(ServiceJob.organization_id == org, ServiceJob.external_id == str(ext))) if ext else None
        scheduled = data.get("scheduled_at")
        completed = data.get("completed_at")
        if isinstance(scheduled, str):
            scheduled = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        if isinstance(completed, str):
            completed = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        status = str(data.get("status") or ("completed" if event_type == "service_job.completed" else "cancelled" if event_type == "service_job.cancelled" else "open"))
        if event_type == "service_job.completed":
            status = "completed"
            completed = completed or now
        elif event_type == "service_job.cancelled":
            status = "cancelled"
        if not service_job:
            service_job = ServiceJob(id=str(data.get("id") or "job_" + uuid.uuid4().hex), organization_id=org, customer_id=customer.id if customer else None, external_id=str(ext) if ext else None, provider=str(data.get("provider") or provider), title=str(data.get("title") or "Service job"), status=status, value=money(data.get("value") or 0), scheduled_at=aware(scheduled), completed_at=aware(completed), created_at=aware(data.get("created_at")) or now, updated_at=now)
            if ext:
                service_job, _ = _safe_insert_or_get(db, ServiceJob, service_job, org, external_value=ext)
            else:
                db.add(service_job); db.flush()
        else:
            service_job.customer_id = customer.id if customer else service_job.customer_id
            service_job.title = str(data.get("title")) if data.get("title") is not None else service_job.title
            service_job.status = status
            service_job.value = money(data.get("value")) if data.get("value") is not None else service_job.value
            service_job.scheduled_at = aware(scheduled) if scheduled is not None else service_job.scheduled_at
            service_job.completed_at = aware(completed) if completed is not None else service_job.completed_at
            service_job.updated_at = now
        remember_identity(customer, data.get("customer_external_id"))
        return {"entity": "service_job", "id": service_job.id, "action": "upserted"}

    if event_type in {"appointment.upserted", "appointment.cancelled", "appointment.no_show"}:
        customer = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        appointment = db.scalar(select(Appointment).where(Appointment.organization_id == org, Appointment.id == str(data.get("id")))) if data.get("id") else None
        status = str(data.get("status") or ("cancelled" if event_type.endswith("cancelled") else "no_show" if event_type.endswith("no_show") else "booked"))
        scheduled = data.get("scheduled_at") or now
        if isinstance(scheduled, str):
            scheduled = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
        if not appointment:
            appointment = Appointment(id=str(data.get("id") or "apt_" + uuid.uuid4().hex), organization_id=org, customer_id=customer.id if customer else None, lead_id=None, status=status, value=money(data.get("value") or 0), scheduled_at=aware(scheduled), cancelled_at=now if status == "cancelled" else None)
            db.add(appointment)
        else:
            appointment.status = status
            appointment.value = money(data.get("value")) if data.get("value") is not None else appointment.value
            appointment.cancelled_at = now if status == "cancelled" else appointment.cancelled_at
        remember_identity(customer, data.get("customer_external_id"))
        return {"entity": "appointment", "id": appointment.id, "action": "upserted"}

    if event_type in {"payment.failed", "payment.succeeded"}:
        customer = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        pid = str(data.get("id") or data.get("external_id") or "pay_" + uuid.uuid4().hex)
        p = db.scalar(select(Payment).where(Payment.organization_id == org, Payment.external_id == str(data.get("external_id")))) if data.get("external_id") else None
        status = "failed" if event_type == "payment.failed" else "succeeded"
        if not p:
            p = Payment(id=pid, organization_id=org, customer_id=customer.id if customer else None, external_id=str(data.get("external_id")) if data.get("external_id") else None, amount=money(data.get("amount") or 0), status=status, failed_at=now if status == "failed" else None, failure_code=data.get("failure_code"))
            if data.get("external_id"):
                p, _ = _safe_insert_or_get(db, Payment, p, org, external_value=data.get("external_id"))
            else:
                db.add(p); db.flush()
        else:
            p.status = status
            p.failed_at = now if status == "failed" else None
        remember_identity(customer, data.get("customer_external_id"))
        return {"entity": "payment", "id": p.id, "action": "upserted"}

    if event_type in {"invoice.upserted", "invoice.created", "invoice.updated", "invoice.overdue", "invoice.paid"}:
        customer = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        ext = data.get("external_id")
        invoice = db.scalar(select(Invoice).where(Invoice.organization_id == org, Invoice.external_id == str(ext))) if ext else None
        due_at = data.get("due_at") or data.get("due_date") or now
        if isinstance(due_at, str):
            due_at = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
        total = money(data.get("total") if data.get("total") is not None else data.get("amount") or 0)
        amount_due = money(data.get("amount_due") if data.get("amount_due") is not None else total)
        amount_due = max(Decimal("0"), min(total, amount_due))
        raw_status = str(data.get("status") or "open").lower()
        status = "paid" if event_type == "invoice.paid" or raw_status in {"paid", "paid_in_full", "completed", "closed", "settled"} else "overdue" if event_type == "invoice.overdue" or raw_status in {"overdue", "past_due", "past-due"} else "void" if raw_status in {"void", "voided", "cancelled", "canceled"} else "open"
        if not invoice:
            invoice = Invoice(
                id=str(data.get("id") or "inv_" + uuid.uuid4().hex), organization_id=org, customer_id=customer.id if customer else None,
                external_id=str(ext) if ext else None, total=total, amount_due=amount_due, status=status, due_at=aware(due_at),
            )
            if ext:
                invoice, _ = _safe_insert_or_get(db, Invoice, invoice, org, external_value=ext)
            else:
                db.add(invoice); db.flush()
        else:
            invoice.customer_id = customer.id if customer else invoice.customer_id
            invoice.total = total
            invoice.amount_due = amount_due
            invoice.status = status
            invoice.due_at = aware(due_at)
        remember_identity(customer, data.get("customer_external_id"))
        return {"entity": "invoice", "id": invoice.id, "action": "upserted"}

    if event_type in {"transaction.paid", "payment.received"}:
        customer = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        tx = db.scalar(select(Transaction).where(Transaction.organization_id == org, Transaction.external_id == str(data.get("external_id")))) if data.get("external_id") else None
        paid_at = data.get("paid_at") or now
        if isinstance(paid_at, str): paid_at = datetime.fromisoformat(paid_at.replace("Z", "+00:00"))
        if not tx:
            tx = Transaction(id=str(data.get("id") or "tx_" + uuid.uuid4().hex), organization_id=org, customer_id=customer.id if customer else None, external_id=str(data.get("external_id")) if data.get("external_id") else None, amount=money(data.get("amount") or 0), status="paid", paid_at=aware(paid_at))
            if data.get("external_id"):
                tx, _ = _safe_insert_or_get(db, Transaction, tx, org, external_value=data.get("external_id"))
            else:
                db.add(tx); db.flush()
        else:
            tx.amount = money(data.get("amount")) if data.get("amount") is not None else tx.amount
            tx.status = "paid"
            tx.paid_at = aware(paid_at)
        remember_identity(customer, data.get("customer_external_id"))
        return {"entity": "transaction", "id": tx.id, "action": "upserted"}

    if event_type in {"review.received", "review.updated"}:
        customer = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        review = db.scalar(select(Review).where(Review.organization_id == org, Review.external_id == str(data.get("external_id")))) if data.get("external_id") else None
        is_new_review = review is None
        if not review:
            review = Review(id=str(data.get("id") or "rev_" + uuid.uuid4().hex), organization_id=org, customer_id=customer.id if customer else None, external_id=str(data.get("external_id")) if data.get("external_id") else None, source=str(data.get("source") or provider), rating=int(data.get("rating") or 5), text=str(data.get("text") or ""), topic=data.get("topic"), severity=data.get("severity"), source_url=data.get("source_url"), created_at=aware(data.get("created_at")) if data.get("created_at") else now)
            if data.get("external_id"):
                review, _ = _safe_insert_or_get(db, Review, review, org, external_value=data.get("external_id"))
            else:
                db.add(review); db.flush()
        else:
            review.rating = int(data.get("rating")) if data.get("rating") is not None else review.rating
            review.text = str(data.get("text")) if data.get("text") is not None else review.text
        remember_identity(customer, data.get("customer_external_id"))
        if is_new_review:
            from .review_system import ReviewPipeline
            ReviewPipeline(db, org).process_review(review)
        return {"entity": "review", "id": review.id, "action": "upserted"}

    if event_type in {"call.missed", "call.abandoned", "call.completed"}:
        customer = find_customer(data.get("customer_external_id"), data.get("customer_email"), data.get("customer_phone"), data.get("customer_id"))
        started = data.get("started_at") or now
        if isinstance(started, str): started = datetime.fromisoformat(started.replace("Z", "+00:00"))
        call = db.scalar(select(CallEvent).where(CallEvent.organization_id == org, CallEvent.external_id == str(data.get("external_id")))) if data.get("external_id") else None
        status = "missed" if event_type == "call.missed" else "abandoned" if event_type == "call.abandoned" else "completed"
        if not call:
            call = CallEvent(id=str(data.get("id") or "call_" + uuid.uuid4().hex), organization_id=org, customer_id=customer.id if customer else None, external_id=str(data.get("external_id")) if data.get("external_id") else None, direction=str(data.get("direction") or "inbound"), status=status, started_at=aware(started), duration_seconds=int(data.get("duration_seconds") or 0))
            if data.get("external_id"):
                call, _ = _safe_insert_or_get(db, CallEvent, call, org, external_value=data.get("external_id"))
            else:
                db.add(call); db.flush()
        else:
            call.status = status
        remember_identity(customer, data.get("customer_external_id"))
        return {"entity": "call", "id": call.id, "action": "upserted"}

    return {"entity": "event", "action": "recorded_only", "event_type": event_type}


def webhook_hash(payload):
    return hashlib.sha256(payload.encode()).hexdigest()
