"""Live provider adapters and canonical synchronization/execution boundary.

Adapters are deliberately provider-specific. They never write directly to the
canonical DB; they return normalized events/actions. The orchestration layer
persists those through canonical_event(), IntegrationEvent and audit logs.
"""
from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Optional, Any
from urllib.parse import urlencode

from .connector_core import BaseAdapter, ConnectorError, ConnectorResult



class StripeAdapter(BaseAdapter):
    provider = "stripe"
    resource_paths = {"customers": (), "payments": (), "invoices": ()}
    base = "https://api.stripe.com/v1"

    def headers(self):
        return {"Authorization": f"Bearer {self.token()}"}

    async def health(self):
        return await self.request("GET", self.base + "/balance", headers=self.headers())

    async def sync(self, resource: str, cursor=None):
        path = {"customers": "/customers", "payments": "/payment_intents", "invoices": "/invoices"}.get(resource)
        if not path:
            raise ConnectorError(f"Stripe resource {resource} is not implemented")
        params = {"limit": 100}
        if cursor:
            params["starting_after"] = cursor
        data = await self.request("GET", self.base + path, headers=self.headers(), params=params)
        records = []
        for x in data.get("data", []):
            if resource == "customers":
                records.append({"type": "customer.upserted", "external_id": x["id"], "data": {"external_id": x["id"], "name": x.get("name") or x.get("email") or "Unknown customer", "email": x.get("email"), "phone": x.get("phone")}})
            elif resource == "payments":
                status = "payment.succeeded" if x.get("status") == "succeeded" else "payment.failed" if x.get("status") in {"canceled", "requires_payment_method"} else None
                if status:
                    records.append({"type": status, "external_id": x["id"], "data": {"external_id": x["id"], "customer_external_id": x.get("customer"), "amount": (x.get("amount") or 0) / 100, "failure_code": (x.get("last_payment_error") or {}).get("code")}})
            else:
                if x.get("status") == "paid":
                    records.append({"type": "transaction.paid", "external_id": x["id"], "data": {"external_id": x["id"], "customer_external_id": x.get("customer"), "amount": (x.get("amount_paid") or 0) / 100, "paid_at": datetime.fromtimestamp(x.get("status_transitions", {}).get("paid_at") or x.get("created") or 0, tz=timezone.utc).isoformat()}})
        next_cursor = data.get("data", [])[-1]["id"] if data.get("has_more") and data.get("data") else None
        return ConnectorResult(self.provider, f"sync.{resource}", records, next_cursor=next_cursor, raw=data)


class TwilioAdapter(BaseAdapter):
    provider = "twilio"
    resource_paths = {"messages": ()}
    base = "https://api.twilio.com/2010-04-01"

    def headers(self):
        sid = self.credentials.get("account_sid")
        secret = self.credentials.get("auth_token") or self.credentials.get("api_key")
        if not sid or not secret:
            raise ConnectorError("Twilio requires account_sid and auth_token/api_key")
        return {"Authorization": "Basic " + base64.b64encode(f"{sid}:{secret}".encode()).decode()}

    async def health(self):
        return await self.request("GET", f"{self.base}/Accounts/{self.credentials.get('account_sid')}.json", headers=self.headers())

    async def send_message(self, to: str, body: str, *, from_: Optional[str] = None, whatsapp=False, status_callback=None):
        sender = from_ or self.credentials.get("whatsapp_from" if whatsapp else "from_number")
        if not sender:
            raise ConnectorError("Twilio sender is not configured")
        if whatsapp and not sender.startswith("whatsapp:"):
            sender = "whatsapp:" + sender
        destination = to if not whatsapp or to.startswith("whatsapp:") else "whatsapp:" + to
        data = {"To": destination, "From": sender, "Body": body}
        if status_callback:
            data["StatusCallback"] = status_callback
        sid = self.credentials.get("account_sid")
        raw = await self.request("POST", f"{self.base}/Accounts/{sid}/Messages.json", headers=self.headers(), data=data)
        return ConnectorResult(self.provider, "send.whatsapp" if whatsapp else "send.sms", [], external_id=raw.get("sid"), raw=raw)

    async def sync(self, resource: str, cursor=None):
        if resource != "messages":
            raise ConnectorError("Twilio adapter currently syncs messages")
        sid = self.credentials.get("account_sid")
        base_url = f"{self.base}/Accounts/{sid}/Messages.json"
        if cursor and str(cursor).startswith("https://"):
            url = str(cursor)
            params = None
        else:
            url = base_url
            params = {"PageSize": min(1000, max(1, int(self.config.get("page_size", 100))))}
            if cursor:
                params["PageToken"] = cursor
        raw = await self.request("GET", url, headers=self.headers(), params=params)
        records = []
        for x in raw.get("messages", []):
            direction = str(x.get("direction", "")).lower()
            inbound = direction.startswith("inbound")
            records.append({"type": "communication.received" if inbound else "communication.sent", "external_id": x.get("sid"), "data": {"external_id": x.get("sid"), "channel": "sms", "customer_phone": x.get("from") if inbound else x.get("to"), "body_preview": x.get("body"), "status": x.get("status"), "occurred_at": x.get("date_sent") or x.get("date_created")}})
        next_uri = raw.get("next_page_uri") or raw.get("nextPageUri")
        if next_uri and not str(next_uri).startswith("https://"):
            next_uri = self.base.split("/2010-04-01")[0] + str(next_uri) if str(next_uri).startswith("/") else None
        return ConnectorResult(self.provider, "sync.messages", records, next_cursor=next_uri, raw=raw)


class GoogleCalendarAdapter(BaseAdapter):
    provider = "google_calendar"
    resource_paths = {"events": ()}
    base = "https://www.googleapis.com/calendar/v3"

    def headers(self):
        return {"Authorization": f"Bearer {self.token()}"}

    async def health(self):
        return await self.request("GET", self.base + "/users/me/calendarList", headers=self.headers(), params={"maxResults": 1})

    async def sync(self, resource: str, cursor=None):
        if resource != "events":
            raise ConnectorError("Google Calendar adapter syncs events")
        if cursor and str(cursor).startswith("https://"):
            url = str(cursor); params = None
        else:
            history_days = max(1, min(3650, int(self.config.get("historical_sync_days", 90))))
            start = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=history_days)
            params = {"singleEvents": "true", "showDeleted": "true", "maxResults": 2500, "orderBy": "startTime", "timeMin": start.isoformat()}
            if self.config.get("sync_all_history"):
                params.pop("timeMin", None)
            if cursor:
                params["pageToken"] = cursor
            url = self.base + "/calendars/primary/events"
        raw = await self.request("GET", url, headers=self.headers(), params=params)
        records = []
        for x in raw.get("items", []):
            start = (x.get("start") or {}).get("dateTime") or (x.get("start") or {}).get("date")
            end = (x.get("end") or {}).get("dateTime") or (x.get("end") or {}).get("date")
            status = "cancelled" if x.get("status") == "cancelled" else "booked"
            records.append({"type": "appointment.upserted" if status == "booked" else "appointment.cancelled", "external_id": x.get("id"), "data": {"id": "apt_ext_" + str(x.get("id")), "external_id": x.get("id"), "customer_email": ((x.get("attendees") or [{}])[0]).get("email"), "scheduled_at": start, "status": status, "title": x.get("summary"), "end_at": end}})
        return ConnectorResult(self.provider, "sync.events", records, next_cursor=raw.get("nextPageToken"), raw=raw)

    async def create_event(self, summary: str, start: str, end: str, attendee_email: Optional[str] = None):
        body = {"summary": summary, "start": {"dateTime": start}, "end": {"dateTime": end}}
        if attendee_email:
            body["attendees"] = [{"email": attendee_email}]
        raw = await self.request("POST", self.base + "/calendars/primary/events", headers=self.headers(), json_body=body)
        return ConnectorResult(self.provider, "create_event", [], external_id=raw.get("id"), raw=raw)


class GmailAdapter(BaseAdapter):
    provider = "google_gmail"
    resource_paths = {"messages": ()}
    base = "https://gmail.googleapis.com/gmail/v1"

    def headers(self):
        return {"Authorization": f"Bearer {self.token()}"}

    async def health(self):
        return await self.request("GET", self.base + "/users/me/profile", headers=self.headers())

    async def send_email(self, to: str, subject: str, body: str):
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode().rstrip("=")
        data = await self.request("POST", self.base + "/users/me/messages/send", headers={**self.headers(), "Content-Type": "application/json"}, json_body={"raw": raw})
        return ConnectorResult(self.provider, "send.email", [], external_id=data.get("id"), raw=data)

    async def sync(self, resource: str, cursor=None):
        if resource != "messages":
            raise ConnectorError("Gmail adapter syncs messages")
        history_days = max(1, min(3650, int(self.config.get("historical_sync_days", 90))))
        q = self.config.get("query") or f"newer_than:{history_days}d"
        params = {"maxResults": min(100, max(1, int(self.config.get("page_size", 50)))), "q": q}
        if cursor:
            params["pageToken"] = cursor
        raw = await self.request("GET", self.base + "/users/me/messages", headers=self.headers(), params=params)
        records = []
        for x in raw.get("messages", []):
            mid = x.get("id")
            if not mid:
                continue
            detail = await self.request("GET", self.base + f"/users/me/messages/{mid}", headers=self.headers(), params={"format":"metadata", "metadataHeaders":["From","To","Subject","Date"]})
            headers = {}
            for h in (detail.get("payload") or {}).get("headers") or []:
                if isinstance(h, dict) and h.get("name"):
                    headers[str(h["name"]).lower()] = h.get("value")
            labels = {str(v) for v in (detail.get("labelIds") or [])}
            inbound = "SENT" not in labels
            records.append({"type": "email.received" if inbound else "email.sent", "external_id": mid, "data": {"external_id": mid, "channel": "email", "customer_email": headers.get("from") if inbound else headers.get("to"), "body_preview": detail.get("snippet") or "", "subject": headers.get("subject"), "status": "received" if inbound else "sent", "occurred_at": headers.get("date") or detail.get("internalDate")}})
        return ConnectorResult(self.provider, "sync.messages", records, next_cursor=raw.get("nextPageToken"), raw=raw)


class HubSpotAdapter(BaseAdapter):
    provider = "hubspot"
    resource_paths = {"contacts": (), "companies": (), "deals": ()}
    base = "https://api.hubapi.com"

    def headers(self):
        return {"Authorization": f"Bearer {self.token()}"}

    async def health(self):
        return await self.request("GET", self.base + "/crm/v3/objects/contacts", headers=self.headers(), params={"limit": 1})

    async def sync(self, resource: str, cursor=None):
        object_name = {"contacts": "contacts", "deals": "deals", "companies": "companies"}.get(resource)
        if not object_name:
            raise ConnectorError(f"HubSpot resource {resource} is not implemented")
        params = {"limit": 100}
        if cursor:
            params["after"] = cursor
        raw = await self.request("GET", f"{self.base}/crm/v3/objects/{object_name}", headers=self.headers(), params=params)
        records = []
        for x in raw.get("results", []):
            p = x.get("properties") or {}
            if resource == "contacts":
                name = " ".join([p.get("firstname") or "", p.get("lastname") or ""]).strip() or p.get("email") or "Unknown customer"
                records.append({"type": "customer.upserted", "external_id": x.get("id"), "data": {"external_id": x.get("id"), "name": name, "email": p.get("email"), "phone": p.get("phone")}})
            elif resource == "deals":
                records.append({"type": "lead.upserted", "external_id": x.get("id"), "data": {"external_id": x.get("id"), "source": "hubspot_deal", "status": p.get("dealstage") or "open", "value": float(p.get("amount") or 0)}})
        return ConnectorResult(self.provider, f"sync.{resource}", records, next_cursor=((raw.get("paging") or {}).get("next") or {}).get("after"), raw=raw)

    async def upsert_contact(self, email: str, firstname: Optional[str] = None, lastname: Optional[str] = None, phone: Optional[str] = None):
        body = {"properties": {k: v for k, v in {"email": email, "firstname": firstname, "lastname": lastname, "phone": phone}.items() if v}}
        raw = await self.request("POST", self.base + "/crm/v3/objects/contacts", headers={**self.headers(), "Content-Type": "application/json"}, json_body=body)
        return ConnectorResult(self.provider, "upsert_contact", [], external_id=raw.get("id"), raw=raw)


class GHLAdapter(BaseAdapter):
    provider = "gohighlevel"
    resource_paths = {"contacts": (), "opportunities": (), "appointments": (), "conversations": ()}
    action_specs = {"send.sms": (), "send.email": (), "send.whatsapp": ()}
    base = "https://services.leadconnectorhq.com"

    def headers(self):
        return {"Authorization": f"Bearer {self.token()}", "Version": self.credentials.get("api_version", "2021-07-28")}

    async def health(self):
        location = self.credentials.get("location_id") or self.config.get("location_id")
        if not location:
            raise ConnectorError("GoHighLevel requires location_id")
        return await self.request("GET", self.base + "/contacts/", headers=self.headers(), params={"locationId": location, "limit": 1})

    async def sync(self, resource: str, cursor=None):
        location = self.credentials.get("location_id") or self.config.get("location_id")
        if not location:
            raise ConnectorError("GoHighLevel requires location_id")
        if resource == "contacts":
            params = {"locationId": location, "limit": 100}
            if cursor: params["startAfterId"] = cursor
            raw = await self.request("GET", self.base + "/contacts/", headers=self.headers(), params=params)
            items = raw.get("contacts", [])
        elif resource == "conversations":
            raw = await self.request("GET", self.base + "/conversations/search", headers=self.headers(), params={"locationId": location, "limit": 100})
            items = raw.get("conversations", [])
        elif resource == "appointments":
            # GHL calendar events require a calendar/user/group identifier. The caller
            # supplies it in integration config; this prevents cross-location reads.
            params = {"locationId": location, "startTime": self.config.get("start_time_ms"), "endTime": self.config.get("end_time_ms")}
            if not params["startTime"] or not params["endTime"]:
                raise ConnectorError("GoHighLevel appointments require start_time_ms and end_time_ms in integration config")
            if self.config.get("calendar_id"): params["calendarId"] = self.config["calendar_id"]
            elif self.config.get("user_id"): params["userId"] = self.config["user_id"]
            elif self.config.get("group_id"): params["groupId"] = self.config["group_id"]
            else: raise ConnectorError("GoHighLevel appointments require calendar_id, user_id, or group_id")
            raw = await self.request("GET", self.base + "/calendars/events", headers=self.headers(), params=params)
            items = raw.get("events", [])
        elif resource == "opportunities":
            params = {"locationId": location, "limit": 100, "page": 1}
            if cursor: params["startAfterId"] = cursor
            raw = await self.request("GET", self.base + "/opportunities/search", headers=self.headers(), params=params)
            items = raw.get("opportunities", [])
        else:
            raise ConnectorError(f"GoHighLevel resource {resource} is not implemented")
        records = []
        for x in items:
            event = None
            if resource == "conversations":
                event = {"type":"communication.received","external_id":x.get("id"),"data":{"external_id":x.get("id"),"channel":x.get("lastMessageType") or "sms","customer_external_id":x.get("contactId"),"body_preview":x.get("lastMessageBody") or "","status":x.get("status") or "received"}}
            else:
                event = {"type":"customer.upserted","external_id":x.get("id"),"data":{"external_id":x.get("id"),"name":" ".join([x.get("firstName") or "",x.get("lastName") or ""]).strip() or x.get("name") or x.get("email") or "Unknown customer","email":x.get("email"),"phone":x.get("phone")}} if resource == "contacts" else self._normalize_resource(resource, x)
            if event: records.append(event)
        return ConnectorResult(self.provider, f"sync.{resource}", records, next_cursor=(raw.get("meta") or {}).get("startAfterId") if isinstance(raw,dict) else None, raw=raw)

    def _normalize_resource(self, resource, x):
        if resource == "opportunities":
            return {"type":"lead.upserted","external_id":x.get("id"),"data":{"external_id":x.get("id"),"source":"gohighlevel","status":x.get("status") or "open","value":x.get("monetaryValue") or 0,"customer_external_id":x.get("contactId")}}
        if resource == "appointments":
            status=str(x.get("appointmentStatus") or x.get("status") or "booked").lower()
            return {"type":"appointment.cancelled" if status == "cancelled" else "appointment.no_show" if status == "noshow" else "appointment.upserted","external_id":x.get("id"),"data":{"id":"apt_gohighlevel_"+str(x.get("id")),"external_id":x.get("id"),"customer_external_id":x.get("contactId"),"scheduled_at":x.get("startTime") or x.get("startTimeUnixNano"),"status":"cancelled" if status == "cancelled" else "no_show" if status == "noshow" else "booked","title":x.get("title")}}
        return None

    async def execute(self, operation, args, *, idempotency_key=None):
        if operation in {"send.sms", "send.email", "send.whatsapp"}:
            location = self.credentials.get("location_id") or self.config.get("location_id")
            if not location: raise ConnectorError("GoHighLevel requires location_id")
            typ = "SMS" if operation == "send.sms" else "Email" if operation == "send.email" else "WhatsApp"
            body = {"type": typ, "contactId": args["contact_id"], "message": args["body"]}
            if operation == "send.email": body["emailFrom"] = args.get("from") or self.config.get("email_from")
            raw = await self.request("POST", self.base + "/conversations/messages", headers={**self.headers(), "Content-Type":"application/json"}, json_body=body)
            return ConnectorResult(self.provider, operation, [], external_id=str((raw.get("message") or raw).get("id") if isinstance((raw.get("message") or raw),dict) else "") or None, raw=raw)
        return await super().execute(operation,args,idempotency_key=idempotency_key)


class GoogleBusinessProfileAdapter(BaseAdapter):
    provider = "google_business_profile"
    resource_paths = {"reviews": ()}
    base = "https://mybusiness.googleapis.com/v4"

    def headers(self):
        return {"Authorization": f"Bearer {self.token()}"}

    async def health(self):
        return await self.request("GET", self.base + "/accounts", headers=self.headers(), params={"pageSize": 1})

    async def sync(self, resource: str, cursor=None):
        if resource != "reviews":
            raise ConnectorError("Google Business Profile adapter syncs reviews")
        accounts = await self.request("GET", self.base + "/accounts", headers=self.headers(), params={"pageSize": 100})
        records = []
        for account in accounts.get("accounts", []):
            account_name = account.get("name")
            locations = await self.request("GET", f"{self.base}/{account_name}/locations", headers=self.headers(), params={"pageSize": 100})
            for loc in locations.get("locations", []):
                loc_name = loc.get("name")
                reviews = await self.request("GET", f"{self.base}/{loc_name}/reviews", headers=self.headers(), params={"pageSize": 50})
                for x in reviews.get("reviews", []):
                    reviewer = x.get("reviewer") or {}
                    records.append({"type": "review.received", "external_id": x.get("reviewId") or x.get("name"), "data": {"external_id": x.get("reviewId") or x.get("name"), "rating": _google_rating(x.get("starRating")), "text": x.get("comment") or "", "customer_name": reviewer.get("displayName"), "source": "google_business_profile", "source_url": x.get("reviewUrl")}})
        return ConnectorResult(self.provider, "sync.reviews", records, raw=accounts)

    async def reply_review(self, review_name: str, comment: str):
        raw = await self.request("PUT", f"https://mybusiness.googleapis.com/v4/{review_name}/reply", headers={**self.headers(), "Content-Type": "application/json"}, json_body={"comment": comment})
        return ConnectorResult(self.provider, "reply_review", [], external_id=review_name, raw=raw)


def _google_rating(value):
    return {"STAR_RATING_UNSPECIFIED": 0, "ONE": 1, "TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5}.get(str(value), int(value) if str(value).isdigit() else 0)


ADAPTERS = {
    "stripe": StripeAdapter,
    "twilio": TwilioAdapter,
    "whatsapp_twilio": TwilioAdapter,
    "google_calendar": GoogleCalendarAdapter,
    "google_gmail": GmailAdapter,
    "hubspot": HubSpotAdapter,
    "gohighlevel": GHLAdapter,
    "google_business_profile": GoogleBusinessProfileAdapter,
}


def adapter_for(provider: str, credentials: dict[str, Any], config: dict[str, Any] | None = None):
    cls = ADAPTERS.get(provider)
    if not cls:
        raise ConnectorError(f"No live adapter installed for {provider}")
    return cls(credentials, config)


# Extended connector fabric is safe to import eagerly because it depends only on
# connector_core, not this module. That keeps the public ADAPTERS registry complete
# regardless of which module is imported first.
from .connector_fabric import FABRIC_ADAPTERS
ADAPTERS.update(FABRIC_ADAPTERS)

# Guardrail: the registry must remain aligned with the provider catalog.
def live_adapter_keys():
    return set(ADAPTERS)
