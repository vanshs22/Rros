"""Runtime manifests and health helpers for production connector orchestration."""
from __future__ import annotations

SYNC_RESOURCES = {
    "stripe": ("customers", "payments", "invoices"),
    "twilio": ("messages",),
    "whatsapp_twilio": ("messages",),
    "google_calendar": ("events",),
    "google_gmail": ("messages",),
    "hubspot": ("contacts", "companies", "deals"),
    "gohighlevel": ("contacts", "opportunities", "appointments", "conversations"),
    "google_business_profile": ("reviews",),
    "microsoft_graph": ("contacts", "messages", "events"),
    "salesforce": ("contacts", "leads", "opportunities"),
    "pipedrive": ("persons", "deals", "activities"),
    "zoho_crm": ("leads", "contacts", "deals", "invoices"),
    "quickbooks": ("customers", "invoices", "payments"),
    "xero": ("contacts", "invoices", "payments"),
    "square": ("customers", "payments", "orders"),
    "shopify": ("customers", "orders", "products"),
    "woocommerce": ("customers", "orders", "products"),
    "calendly": ("scheduled_events",),
    "slack": ("channels", "messages", "users"),
    "microsoft_teams": ("messages",),
    "servicetitan": ("customers", "jobs", "appointments", "estimates", "invoices", "payments"),
    "housecall_pro": ("customers", "jobs", "appointments", "invoices", "payments"),
    "jobber": ("clients", "jobs", "appointments", "invoices"),
    "jobnimbus": ("contacts", "jobs", "tasks"),
    "fieldedge": ("customers", "leads", "work_orders", "appointments", "invoices", "payments"),
    "acculynx": ("companies", "jobs", "estimates", "invoices"),
    "gorilladesk": ("customers", "jobs", "appointments", "invoices", "payments"),
    "dynamics_365": ("contacts", "leads", "opportunities"),
    "freshsales": ("contacts", "deals", "activities"),
    "close": ("leads", "contacts", "activities", "opportunities"),
    "keap": ("contacts", "opportunities", "orders", "appointments"),
    "zendesk": ("tickets", "users", "organizations"),
    "intercom": ("contacts", "conversations", "tickets"),
    "yelp": ("reviews", "businesses"),
}

# Explicit capability truth: a provider is only considered executable if its adapter
# advertises a concrete operation/resource instead of being registry metadata alone.
def adapter_contract(adapter_cls) -> dict:
    if adapter_cls is None:
        return {"resources": (), "actions": (), "has_sync": False, "has_execute": False}
    resources = tuple((getattr(adapter_cls, "resource_paths", {}) or {}).keys())
    actions = set((getattr(adapter_cls, "action_specs", {}) or {}).keys())
    # Mature adapters implement these operations as explicit methods instead of
    # action_specs. Expose them in the same contract so the API can reject invalid
    # actions before a job is queued.
    if hasattr(adapter_cls, "send_message"):
        actions.update({"send.sms", "send.whatsapp"})
    if hasattr(adapter_cls, "send_email"):
        actions.add("send.email")
    if hasattr(adapter_cls, "create_event"):
        actions.add("create_event")
    if hasattr(adapter_cls, "upsert_contact"):
        actions.add("upsert_contact")
    if hasattr(adapter_cls, "reply_review"):
        actions.add("reply_review")
    return {"resources": resources, "actions": tuple(sorted(actions)), "has_sync": hasattr(adapter_cls, "sync"), "has_execute": hasattr(adapter_cls, "execute")}
