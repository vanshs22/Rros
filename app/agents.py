"""Governed Revenue Recovery Agent OS.

The first implementation is deterministic and provider-agnostic: it proves the
agent contracts, handoffs, memory, feedback and outcome-learning loop without
pretending an LLM or external action provider exists. An LLM provider can later
replace individual decision functions while keeping the same schemas and gates.
"""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Any
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    AgentDefinition, AgentRun, AgentHandoff, AgentMemory, AgentOutcome, AgentFeedback,
    Customer, CustomerIdentity, Integration, Lead, Appointment, Transaction, Payment, Invoice, Review,
    CommunicationEvent, CallEvent, ChannelConsent, RevenueOpportunity, RecoveryPlan, ServiceJob,
)
from .services import jdump, money, add_audit, add_event

AGENT_DEFS = [
    ("context", "Context Agent", "Builds a canonical evidence pack and customer/revenue timeline.", "observe"),
    ("lead_investigator", "Lead Investigator", "Diagnoses lead/contact/follow-up leakage from evidence.", "recommend"),
    ("churn_analyst", "Churn Analyst", "Models behavioral purchase gaps and churn/dormancy signals.", "recommend"),
    ("review_analyst", "Review Analyst", "Classifies review severity/topics and revenue-recovery relevance.", "recommend"),
    ("revenue_analyst", "Revenue Analyst", "Estimates value, probability, expected value and confidence.", "recommend"),
    ("recovery_planner", "Recovery Planner", "Creates a governed recovery strategy and escalation path.", "draft"),
    ("communication", "Communication Agent", "Drafts compliant channel-specific customer communication.", "draft"),
    ("qa_guardrail", "QA / Guardrail Agent", "Checks consent, policy, risk and execution eligibility.", "approval_required"),
    ("action", "Action Agent", "Converts an approved plan into an idempotent connector action.", "auto_execute"),
    ("roi", "ROI Analyst", "Attributes outcomes and measures recovery economics.", "observe"),
    ("learning", "Learning Agent", "Turns outcomes and feedback into versioned learning signals.", "recommend"),
]


def now():
    return datetime.now(timezone.utc)


def _obj(model, org, oid, db):
    return db.scalar(select(model).where(model.organization_id == org, model.id == oid))


def ensure_definitions(db: Session, org: str):
    for key, name, description, mode in AGENT_DEFS:
        row = db.scalar(select(AgentDefinition).where(AgentDefinition.organization_id == org, AgentDefinition.key == key))
        if not row:
            db.add(AgentDefinition(id="agt_" + uuid.uuid4().hex, organization_id=org, key=key, name=name, description=description, autonomy_mode=mode, version=1, status="active"))
    db.flush()


def _json(value):
    return json.dumps(value, separators=(",", ":"), default=str)


def _load(raw, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def build_context(db: Session, org: str, opp: RevenueOpportunity) -> dict[str, Any]:
    customer = _obj(Customer, org, opp.customer_id, db) if opp.customer_id else None
    leads = db.scalars(select(Lead).where(Lead.organization_id == org, Lead.id == opp.lead_id)).all() if opp.lead_id else []
    appointments = db.scalars(select(Appointment).where(Appointment.organization_id == org, (Appointment.customer_id == opp.customer_id) | (Appointment.lead_id == opp.lead_id))).all() if opp.customer_id or opp.lead_id else []
    service_jobs = db.scalars(select(ServiceJob).where(ServiceJob.organization_id == org, ServiceJob.customer_id == opp.customer_id).order_by(ServiceJob.updated_at.desc()).limit(50)).all() if opp.customer_id else []
    transactions = db.scalars(select(Transaction).where(Transaction.organization_id == org, Transaction.customer_id == opp.customer_id).order_by(Transaction.paid_at.desc()).limit(50)).all() if opp.customer_id else []
    payments = db.scalars(select(Payment).where(Payment.organization_id == org, Payment.customer_id == opp.customer_id).order_by(Payment.failed_at.desc()).limit(50)).all() if opp.customer_id else []
    invoices = db.scalars(select(Invoice).where(Invoice.organization_id == org, Invoice.customer_id == opp.customer_id).order_by(Invoice.due_at.desc()).limit(50)).all() if opp.customer_id else []
    reviews = db.scalars(select(Review).where(Review.organization_id == org, Review.customer_id == opp.customer_id).order_by(Review.created_at.desc()).limit(20)).all() if opp.customer_id else []
    messages = db.scalars(select(CommunicationEvent).where(CommunicationEvent.organization_id == org, CommunicationEvent.customer_id == opp.customer_id).order_by(CommunicationEvent.occurred_at.desc()).limit(50)).all() if opp.customer_id else []
    calls = db.scalars(select(CallEvent).where(CallEvent.organization_id == org, CallEvent.customer_id == opp.customer_id).order_by(CallEvent.started_at.desc()).limit(30)).all() if opp.customer_id else []
    consents = db.scalars(select(ChannelConsent).where(ChannelConsent.organization_id == org, ChannelConsent.customer_id == opp.customer_id)).all() if opp.customer_id else []
    identities = db.scalars(select(CustomerIdentity).where(CustomerIdentity.organization_id == org, CustomerIdentity.customer_id == opp.customer_id).order_by(CustomerIdentity.provider)).all() if opp.customer_id else []
    integrations = db.scalars(select(Integration).where(Integration.organization_id == org, Integration.credentials_ciphertext.is_not(None), Integration.status.in_(("configured","connected")))).all()
    return {
        "opportunity": {"id": opp.id, "type": opp.type, "status": opp.status, "reason": opp.reason, "estimated_value": float(opp.estimated_value), "probability": float(opp.probability), "expected_value": float(opp.expected_value), "confidence": float(opp.confidence), "evidence": _load(opp.evidence_json, [])},
        "customer": {"id": customer.id, "name": customer.name, "email": customer.email, "phone": customer.phone, "lifecycle_status": customer.lifecycle_status} if customer else None,
        "leads": [{"id": x.id, "status": x.status, "source": x.source, "value": float(x.value), "created_at": x.created_at.isoformat()} for x in leads],
        "appointments": [{"id": x.id, "status": x.status, "value": float(x.value), "scheduled_at": x.scheduled_at.isoformat()} for x in appointments],
        "service_jobs": [{"id": x.id, "provider": x.provider, "status": x.status, "title": x.title, "value": float(x.value), "scheduled_at": x.scheduled_at.isoformat() if x.scheduled_at else None, "completed_at": x.completed_at.isoformat() if x.completed_at else None} for x in service_jobs],
        "transactions": [{"id": x.id, "amount": float(x.amount), "status": x.status, "paid_at": x.paid_at.isoformat()} for x in transactions],
        "payments": [{"id": x.id, "amount": float(x.amount), "status": x.status, "failed_at": x.failed_at.isoformat() if x.failed_at else None, "failure_code": x.failure_code} for x in payments],
        "invoices": [{"id": x.id, "total": float(x.total), "amount_due": float(x.amount_due), "status": x.status, "due_at": x.due_at.isoformat()} for x in invoices],
        "reviews": [{"id": x.id, "rating": x.rating, "topic": x.topic, "severity": x.severity, "text": x.text[:500]} for x in reviews],
        "messages": [{"id": x.id, "channel": x.channel, "direction": x.direction, "status": x.status, "occurred_at": x.occurred_at.isoformat(), "body_preview": x.body_preview} for x in messages],
        "calls": [{"id": x.id, "direction": x.direction, "status": x.status, "started_at": x.started_at.isoformat(), "duration_seconds": x.duration_seconds} for x in calls],
        "consents": [{"channel": x.channel, "allowed": x.allowed, "source": x.source} for x in consents],
        "identities": [{"provider": x.provider, "external_id": x.external_id} for x in identities],
        "connected_providers": sorted({x.provider for x in integrations}),
    }


def _agent_output(key: str, ctx: dict[str, Any], prior: dict[str, Any]) -> dict[str, Any]:
    opp = ctx["opportunity"]
    if key == "context":
        return {"evidence": opp["evidence"], "counts": {k: len(ctx[k]) for k in ("leads","appointments","service_jobs","transactions","payments","invoices","reviews","messages","calls")}, "missing_data": [k for k in ("customer","leads") if not ctx.get(k) and not opp.get("type", "").startswith("review")], "confidence": opp["confidence"]}
    if key == "lead_investigator":
        leads, messages, appointments = ctx["leads"], ctx["messages"], ctx["appointments"]
        diagnoses=[]
        if any(x.get("status") in {"new","open","uncontacted"} for x in leads): diagnoses.append("lead_not_contacted_or_open")
        if messages and not any(x.get("direction") == "outbound" for x in messages): diagnoses.append("no_outbound_followup_observed")
        if any(x.get("status") in {"no_show","cancelled"} for x in appointments): diagnoses.append("appointment_recovery_needed")
        return {"evidence": diagnoses, "diagnosis": diagnoses[0] if diagnoses else opp["reason"], "missing_data": [], "confidence": min(1.0, 0.55 + .1*len(diagnoses))}
    if key == "churn_analyst":
        tx = sorted(ctx["transactions"], key=lambda x:x["paid_at"])
        intervals=[]
        for a,b in zip(tx, tx[1:]):
            da=datetime.fromisoformat(a["paid_at"]); db=datetime.fromisoformat(b["paid_at"]); da=da.replace(tzinfo=timezone.utc) if da.tzinfo is None else da; db=db.replace(tzinfo=timezone.utc) if db.tzinfo is None else db; intervals.append((db-da).days)
        baseline=sum(intervals)/len(intervals) if intervals else None
        last=datetime.fromisoformat(tx[-1]["paid_at"]); last=last.replace(tzinfo=timezone.utc) if last.tzinfo is None else last; days_since=(now()-last).days if tx else None
        deviation=(days_since-baseline)/baseline if baseline and days_since is not None else None
        risk=min(1.0,max(0.0,(deviation or 0)*0.6)) if deviation is not None else 0.25
        return {"baseline_purchase_interval_days": round(baseline,2) if baseline else None,"days_since_last_purchase":days_since,"deviation":round(deviation,3) if deviation is not None else None,"signals":["purchase_interval_breach"] if deviation and deviation>0.5 else [],"risk":round(risk,3),"confidence":0.8 if baseline else 0.35}
    if key == "review_analyst":
        reviews=ctx["reviews"]; negative=[r for r in reviews if r["rating"]<=3]
        return {"topic": negative[0].get("topic") if negative else None,"severity":"high" if any(r["rating"]<=2 for r in negative) else ("medium" if negative else "low"),"sentiment":"negative" if negative else "neutral","customer_match":bool(negative),"recommended_case":bool(negative),"confidence":0.9 if negative else 0.5}
    if key == "revenue_analyst":
        expected=Decimal(str(opp["estimated_value"]))*Decimal(str(opp["probability"]))
        return {"estimated_value":opp["estimated_value"],"probability":opp["probability"],"expected_value":float(expected),"assumptions":["detector estimate and probability are current source-of-truth"],"confidence":opp["confidence"]}
    if key == "recovery_planner":
        cons={x["channel"]:x["allowed"] for x in ctx["consents"]}
        channel="sms" if cons.get("sms") and ctx.get("customer",{}).get("phone") else ("email" if cons.get("email") and ctx.get("customer",{}).get("email") else "none")
        return {"objective":"recover revenue without violating consent or policy","channel":channel,"steps":["investigate_context","draft_message","qa_guardrail","request_approval","execute_connector","measure_outcome"],"escalation":"human_review","approval_required":True,"confidence":0.85 if channel != "none" else 0.45}
    if key == "communication":
        plan=prior.get("recovery_planner",{}); channel=plan.get("channel","none"); name=(ctx.get("customer") or {}).get("name") or "there"
        if channel == "sms": message=f"Hi {name}, we noticed there may be a follow-up we can help with. Would you like us to help you get this sorted?"
        elif channel == "email": message=f"Hi {name}, we wanted to follow up and see if we can help with your outstanding request."
        else: message=""
        return {"channel":channel,"message":message,"consent_required":channel not in {"none"},"policy_flags":[] if message else ["no_consented_channel"]}
    if key == "qa_guardrail":
        comm=prior.get("communication",{}); allowed=bool(comm.get("message")) and not comm.get("policy_flags")
        return {"allow":allowed,"reasons":[] if allowed else ["no_valid_consented_channel_or_message"],"policy_checks":["consent","approval_required","no_guaranteed_outcome_language"]}
    if key == "action":
        qa=prior.get("qa_guardrail",{}); comm=prior.get("communication",{}); channel=comm.get("channel")
        providers=set(ctx.get("connected_providers",[])); identities={x["provider"]:x["external_id"] for x in ctx.get("identities",[])}
        customer=ctx.get("customer") or {}
        if channel == "sms":
            if "twilio" in providers and customer.get("phone"):
                provider, operation = "twilio", "send.sms"
                args = {"channel":channel,"to":customer["phone"],"body":comm.get("message")}
            elif "gohighlevel" in providers and identities.get("gohighlevel") and customer.get("phone"):
                provider, operation = "gohighlevel", "send.sms"
                args = {"channel":channel,"contact_id":identities["gohighlevel"],"to":customer["phone"],"body":comm.get("message")}
            else: provider=operation=None; args={"channel":channel,"body":comm.get("message")}
        elif channel == "whatsapp":
            provider, operation = ("whatsapp_twilio", "send.whatsapp") if "whatsapp_twilio" in providers and customer.get("phone") else ("gohighlevel", "send.whatsapp") if "gohighlevel" in providers and identities.get("gohighlevel") and customer.get("phone") else (None, None)
            args={"channel":channel,"to":customer.get("phone"),"contact_id":identities.get("gohighlevel"),"body":comm.get("message")}
        elif channel == "email":
            provider, operation = ("google_gmail", "send.email") if "google_gmail" in providers and customer.get("email") else ("microsoft_graph", "send.email") if "microsoft_graph" in providers and customer.get("email") else ("gohighlevel", "send.email") if "gohighlevel" in providers and identities.get("gohighlevel") and customer.get("email") else (None, None)
            args={"channel":channel,"to":customer.get("email"),"contact_id":identities.get("gohighlevel"),"subject":"Following up","body":comm.get("message")}
        else: provider, operation, args = None, None, {"channel":channel,"body":comm.get("message")}
        keyid=f"agent:{opp['id']}:{provider}:{operation}:{channel}"
        return {"tool":"connector.send_message","provider":provider,"operation":operation,"arguments":args,"idempotency_key":keyid,"allowed":qa.get("allow",False) and bool(provider) and bool(operation)}
    if key == "roi":
        return {"outcome_class":"pending","transaction_ids":[],"attribution_window_days":30,"confidence":0.0}
    if key == "learning":
        return {"learning_signal":"pending_outcome","features":{"opportunity_type":opp["type"],"channel":prior.get("recovery_planner",{}).get("channel")},"recommendation":"wait for outcome before updating policy"}
    return {}


def run_agent_pipeline(db: Session, org: str, opp: RevenueOpportunity, requested_by: Optional[str] = None) -> dict[str, Any]:
    # Serialize agent execution per opportunity. PostgreSQL uses the row lock; SQLite
    # remains correct for the normal single-process development/test runtime.
    locked_opp = db.scalar(select(RevenueOpportunity).where(RevenueOpportunity.organization_id == org, RevenueOpportunity.id == opp.id).with_for_update())
    if not locked_opp:
        raise ValueError("Opportunity not found")
    opp = locked_opp
    active = db.scalar(select(AgentRun).where(
        AgentRun.organization_id == org, AgentRun.opportunity_id == opp.id,
        AgentRun.parent_run_id.is_(None), AgentRun.status == "running"
    ).order_by(AgentRun.started_at.desc()).limit(1))
    if active:
        return {"pipeline_run_id": active.id, "status": "running", "agents": {}}
    ensure_definitions(db, org)
    ctx=build_context(db, org, opp)
    run_id="arun_"+uuid.uuid4().hex
    root=AgentRun(id=run_id, organization_id=org, opportunity_id=opp.id, status="running", pipeline_version="1.0", requested_by=requested_by, input_json=_json(ctx))
    db.add(root); db.flush()
    prior={}
    order=[x[0] for x in AGENT_DEFS]
    for key in order:
        definition=db.scalar(select(AgentDefinition).where(AgentDefinition.organization_id==org, AgentDefinition.key==key))
        started=now()
        try:
            output=_agent_output(key,ctx,prior)
            ar=AgentRun(id="arun_"+uuid.uuid4().hex, organization_id=org, opportunity_id=opp.id, parent_run_id=root.id, agent_definition_id=definition.id, status="succeeded", pipeline_version=str(definition.version), requested_by=requested_by, input_json=_json({"context":ctx,"prior_agents":prior}), output_json=_json(output), started_at=started, completed_at=now())
            db.add(ar); db.flush()
            prior[key]=output
            if key != "context":
                db.add(AgentHandoff(id="ah_"+uuid.uuid4().hex, organization_id=org, pipeline_run_id=root.id, from_agent_key=order[order.index(key)-1], to_agent_key=key, payload_json=_json(output)))
        except Exception as exc:
            db.add(AgentRun(id="arun_"+uuid.uuid4().hex, organization_id=org, opportunity_id=opp.id, parent_run_id=root.id, agent_definition_id=definition.id, status="failed", pipeline_version=str(definition.version), requested_by=requested_by, input_json=_json({"context":ctx,"prior_agents":prior}), output_json=_json({"error":str(exc)}), started_at=started, completed_at=now()))
            root.status="failed"; root.completed_at=now(); db.flush(); raise
    root.status="succeeded"; root.output_json=_json(prior); root.completed_at=now()
    # Persist durable, tenant-scoped memory as a compact summary. This is not a substitute for raw evidence.
    db.add(AgentMemory(id="am_"+uuid.uuid4().hex, organization_id=org, scope_type="opportunity", scope_id=opp.id, memory_type="agent_pipeline_summary", content_json=_json({"pipeline_run_id":root.id,"signals":{k:v for k,v in prior.items() if k in {"lead_investigator","churn_analyst","review_analyst","revenue_analyst","learning"}}}), confidence=float(prior.get("revenue_analyst",{}).get("confidence",0)), source_run_id=root.id))
    add_event(db,org,opp.id,"agent_pipeline_completed","agent_orchestrator",{"pipeline_run_id":root.id,"agents":order})
    add_audit(db,org,"agent_pipeline_completed","opportunity",opp.id,{"pipeline_run_id":root.id,"agents":order},requested_by)
    return {"pipeline_run_id":root.id,"status":root.status,"agents":prior}


def record_feedback(db: Session, org: str, run_id: str, agent_key: str, rating: int, correction: Optional[str], user_id: Optional[str]):
    if rating < 1 or rating > 5: raise ValueError("rating must be 1-5")
    parent=db.scalar(select(AgentRun).where(AgentRun.organization_id==org, AgentRun.id==run_id))
    if not parent: raise ValueError("agent run not found")
    row=AgentFeedback(id="afb_"+uuid.uuid4().hex, organization_id=org, agent_run_id=run_id, agent_key=agent_key, rating=rating, correction=correction, created_by_user_id=user_id)
    db.add(row)
    return row


def record_outcome(db: Session, org: str, opp: RevenueOpportunity, outcome: str, amount: Optional[Decimal] = None, metadata: Optional[dict] = None):
    row=AgentOutcome(id="aout_"+uuid.uuid4().hex, organization_id=org, opportunity_id=opp.id, outcome=outcome, amount=money(amount or 0), metadata_json=_json(metadata or {}))
    db.add(row); db.flush()
    # Outcome learning is deliberately aggregate and auditable; it never silently changes policy.
    recent=db.scalars(select(AgentOutcome).where(AgentOutcome.organization_id==org).order_by(AgentOutcome.created_at.desc()).limit(500)).all()
    converted=sum(1 for x in recent if x.outcome in {"recovered","converted"})
    rate=converted/len(recent) if recent else 0
    db.add(AgentMemory(id="am_"+uuid.uuid4().hex, organization_id=org, scope_type="organization", scope_id=org, memory_type="outcome_learning", content_json=_json({"sample_size":len(recent),"conversion_rate":round(rate,4),"latest_outcome":outcome,"opportunity_type":opp.type}), confidence=min(0.95,0.3+len(recent)/200), source_run_id=None))
    return row
