"""Production connector fabric for the full RROS provider catalog.

The fabric deliberately separates provider transport from canonical persistence.
Every adapter returns canonical event envelopes; the orchestration layer persists
those envelopes with tenant-scoped idempotency. No adapter receives a database
session and no provider payload is allowed to become a domain object directly.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Any
from urllib.parse import quote

from .connector_core import BaseAdapter, ConnectorError, ConnectorResult


class FabricAdapter(BaseAdapter):
    supports_idempotency = False
    """Common production transport for REST/GraphQL providers."""
    provider = ""
    base = ""

    def _base(self) -> str:
        base = self.config.get("base_url") or self.credentials.get("base_url") or self.base
        if not base:
            raise ConnectorError(f"{self.provider} requires an account-specific base_url")
        if not base.startswith("https://"):
            raise ConnectorError("Connector base_url must use HTTPS")
        return base.rstrip("/")

    def headers(self) -> dict[str, str]:
        token = self.token()
        if not token:
            raise ConnectorError(f"{self.provider} credentials are missing an access token")
        return {"Authorization": f"Bearer {token}"}

    async def health(self):
        return await self.request("GET", self._url(self.health_path), headers=self.headers(), params=self.health_params())

    health_path = "/"

    def health_params(self):
        return {}

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            if not path.startswith("https://"):
                raise ConnectorError("Connector URLs must use HTTPS")
            return path
        return self._base() + "/" + path.lstrip("/")

    @staticmethod
    def _format_path(template: str, args: dict[str, Any]) -> str:
        def repl(match):
            key = match.group(1)
            if key not in args or args[key] in (None, ""):
                raise ConnectorError(f"Missing path argument: {key}")
            return quote(str(args[key]), safe="")
        return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", repl, template)

    @staticmethod
    def _cursor(raw: dict[str, Any], cursor_key: str = "next") -> Optional[str]:
        for obj in (raw.get("pagination"), raw.get("meta"), raw.get("paging")):
            if isinstance(obj, dict):
                for key in (cursor_key, "next_page_token", "next_cursor", "nextPageToken", "after", "startAfterId"):
                    value = obj.get(key)
                    if value:
                        return str(value)
                nxt = obj.get("next") or obj.get("next_page") or obj.get("nextPage")
                if isinstance(nxt, str):
                    return nxt
        for key in ("next_cursor", "nextPageToken", "next_page_token", "after"):
            if raw.get(key):
                return str(raw[key])
        return None

    @staticmethod
    def _items(raw: Any, *keys: str) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            return [x for x in raw if isinstance(x, dict)]
        if not isinstance(raw, dict):
            return []
        for key in keys:
            value = raw.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        for value in raw.values():
            if isinstance(value, list) and all(isinstance(x, dict) for x in value):
                return value
        return []

    @staticmethod
    def _first(d: dict[str, Any], *keys: str):
        for key in keys:
            cur: Any = d
            ok = True
            for part in key.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    ok = False
                    break
                cur = cur[part]
            if ok and cur not in (None, ""):
                return cur
        return None

    def normalize(self, resource: str, x: dict[str, Any]) -> dict[str, Any] | None:
        ext = self._first(x, "id", "uuid", "uri", "resource.id", "external_id")
        if not ext:
            return None
        ext = str(ext)
        low = resource.lower()
        email = self._first(x, "email", "primary_email", "customer.email", "contact.email", "person.email")
        phone = self._first(x, "phone", "primary_phone", "customer.phone", "contact.phone", "person.phone")
        name = self._first(x, "name", "display_name", "full_name", "customer.name", "contact.name", "person.name")
        if not name:
            name = " ".join(str(v) for v in [self._first(x, "first_name", "firstname", "customer.first_name", "contact.first_name"), self._first(x, "last_name", "lastname", "customer.last_name", "contact.last_name")] if v).strip() or email or "Unknown customer"
        if any(k in low for k in ("review", "reviews")):
            return {"type": "review.received", "external_id": ext, "data": {"external_id": ext, "source": self.provider, "customer_email": email, "customer_phone": phone, "rating": self._first(x, "rating", "stars", "score") or 5, "text": self._first(x, "text", "comment", "body", "content") or "", "created_at": self._first(x, "created_at", "create_time", "published_at")}}
        if any(k in low for k in ("invoice", "invoices")):
            status = str(self._first(x, "status", "payment_status", "state") or "open").lower()
            typ = "invoice.paid" if status in {"paid", "paid_in_full", "completed", "closed", "settled", "success"} else "invoice.overdue" if status in {"overdue", "past_due", "past-due"} else "invoice.upserted"
            return {"type": typ, "external_id": ext, "data": {"id": f"inv_{self.provider}_{ext}", "external_id": ext, "customer_external_id": self._first(x, "customer_id", "customer.id", "contact_id", "contact.id", "customerRef", "customerRef.id"), "customer_email": email, "customer_phone": phone, "total": self._first(x, "total", "total_amount", "amount", "value") or 0, "amount_due": self._first(x, "amount_due", "balance", "amount_due_amount", "amountRemaining") if self._first(x, "amount_due", "balance", "amount_due_amount", "amountRemaining") is not None else (self._first(x, "total", "total_amount", "amount", "value") or 0), "status": status, "due_at": self._first(x, "due_at", "due_date", "dueDate", "date_due", "expiration_date"), "created_at": self._first(x, "created_at", "create_time")}}
        if any(k in low for k in ("payment", "payments")):
            status = str(self._first(x, "status", "payment_status") or "").lower()
            typ = "payment.succeeded" if status in {"succeeded", "completed", "paid", "captured", "settled"} else "payment.failed" if status in {"failed", "canceled", "cancelled", "declined", "voided"} else "payment.succeeded"
            return {"type": typ, "external_id": ext, "data": {"external_id": ext, "customer_external_id": self._first(x, "customer_id", "customer.id", "contact_id", "contact.id"), "customer_email": email, "customer_phone": phone, "amount": self._first(x, "amount", "amount_paid", "total", "value", "total_amount") or 0, "failure_code": self._first(x, "failure_code", "error_code")}}
        if any(k in low for k in ("order", "orders", "transaction", "transactions")):
            status = str(self._first(x, "status", "payment_status") or "").lower()
            if status in {"paid", "paid_in_full", "completed", "closed", "settled", "success"}:
                return {"type": "transaction.paid", "external_id": ext, "data": {"external_id": ext, "customer_external_id": self._first(x, "customer_id", "customer.id", "contact_id", "contact.id"), "customer_email": email, "customer_phone": phone, "amount": self._first(x, "amount_paid", "total", "total_amount", "amount", "value") or 0, "paid_at": self._first(x, "paid_at", "payment_date", "updated_at", "created_at")}}
        if any(k in low for k in ("message", "messages", "conversation", "conversations", "email")):
            inbound = str(self._first(x, "direction", "type") or "").lower() in {"inbound", "received", "incoming"}
            return {"type": "communication.received" if inbound else "communication.sent", "external_id": ext, "data": {"external_id": ext, "channel": self._first(x, "channel", "type") or "email", "customer_email": email, "customer_phone": phone, "body_preview": self._first(x, "body", "text", "content", "preview") or "", "status": self._first(x, "status") or "received", "occurred_at": self._first(x, "occurred_at", "created_at", "timestamp")}}
        # Field-service jobs/work orders have a different grain from appointments.
        # Preserve them as service_job events so jobs without a scheduled visit do not
        # become false appointments in the revenue graph.
        if any(k in low for k in ("job", "jobs", "work_order", "work_orders")) and not any(k in low for k in ("appointment", "appointments", "booking", "bookings")):
            status = str(self._first(x, "status", "state", "job_status", "milestone") or "open").lower()
            scheduled = self._first(x, "scheduled_at", "start_time", "startTime", "appointment_date", "scheduledStart", "initialAppointment.startAt")
            completed = self._first(x, "completed_at", "completedAt", "closed_at", "closedAt")
            return {"type": "service_job.completed" if status in {"completed", "complete", "closed", "done"} else "service_job.upserted", "external_id": ext, "data": {"external_id": ext, "provider": self.provider, "customer_external_id": self._first(x, "customer_id", "customer.id", "contact_id", "contact.id", "client_id", "client.id", "company_id"), "customer_email": email, "customer_phone": phone, "title": self._first(x, "title", "summary", "name", "job_name", "description"), "status": status, "value": self._first(x, "value", "amount", "job_value", "estimate_total", "total") or 0, "scheduled_at": scheduled, "completed_at": completed, "created_at": self._first(x, "created_at", "createdDate", "created_at_utc")}}
        if any(k in low for k in ("appointment", "appointments", "event", "events", "booking", "bookings", "invitee", "invitees")):
            status = str(self._first(x, "status", "state") or "booked").lower()
            status = "cancelled" if status in {"cancelled", "canceled"} else "no_show" if status in {"no_show", "no-show"} else "booked"
            when = self._first(x, "scheduled_at", "start_time", "startTime", "start", "date", "appointment_date", "created_at") or datetime.now(timezone.utc).isoformat()
            return {"type": "appointment.cancelled" if status == "cancelled" else "appointment.no_show" if status == "no_show" else "appointment.upserted", "external_id": ext, "data": {"id": f"apt_{self.provider}_{ext}", "external_id": ext, "customer_external_id": self._first(x, "customer_id", "customer.id", "contact_id", "contact.id"), "customer_email": email, "customer_phone": phone, "scheduled_at": when, "status": status, "title": self._first(x, "title", "summary", "name", "job_name")}}
        if any(k in low for k in ("lead", "leads", "deal", "deals", "opportunity", "opportunities")):
            return {"type": "lead.upserted", "external_id": ext, "data": {"external_id": ext, "source": self.provider, "status": self._first(x, "status", "stage", "dealstage") or "open", "value": self._first(x, "amount", "value", "deal_value") or 0, "customer_email": email, "customer_phone": phone}}
        if any(k in low for k in ("call", "calls")):
            status = str(self._first(x, "status", "disposition") or "completed").lower()
            typ = "call.missed" if status in {"missed", "no_answer", "no-answer"} else "call.completed"
            return {"type": typ, "external_id": ext, "data": {"external_id": ext, "customer_email": email, "customer_phone": phone, "direction": self._first(x, "direction") or "inbound", "started_at": self._first(x, "started_at", "start_time", "created_at"), "duration_seconds": self._first(x, "duration_seconds", "duration") or 0}}
        if any(k in low for k in ("customer", "customers", "contact", "contacts", "person", "persons", "company", "companies", "account", "accounts", "client", "clients")):
            return {"type": "customer.upserted", "external_id": ext, "data": {"external_id": ext, "name": name, "email": email, "phone": phone}}
        return None

    async def sync(self, resource: str, cursor: Optional[str] = None):
        path, params, keys = self.resource_request(resource, cursor)
        raw = await self.request("GET", self._url(path), headers=self.headers(), params=params)
        records = []
        for item in self._items(raw, *keys):
            event = self.normalize(resource, item)
            if event:
                records.append(event)
        return ConnectorResult(self.provider, f"sync.{resource}", records, next_cursor=self.next_cursor(raw), raw=raw if isinstance(raw, dict) else {"data": raw})

    def resource_request(self, resource: str, cursor: Optional[str]):
        mapping = self.config.get("resource_paths", {})
        spec = mapping.get(resource) if isinstance(mapping, dict) else None
        if not spec:
            spec = self.resource_paths.get(resource)
        if not spec:
            raise ConnectorError(f"{self.provider} resource {resource} is not implemented")
        path = spec[0] if isinstance(spec, (list, tuple)) else spec
        keys = tuple(spec[1]) if isinstance(spec, (list, tuple)) and len(spec) > 1 else ("data", resource)
        params: dict[str, Any] = dict(self.config.get("resource_params", {}).get(resource, {})) if isinstance(self.config.get("resource_params"), dict) else {}
        if cursor:
            if cursor.startswith("http"):
                path = cursor
            elif self.pagination_mode == "after": params["after"] = cursor
            elif self.pagination_mode == "page_token": params["page_token"] = cursor
            elif self.pagination_mode == "start_after": params["startAfterId"] = cursor
            else: params[self.page_param] = cursor
        params.setdefault(self.limit_param, self.page_size)
        return path, params, keys

    resource_paths: dict[str, Any] = {}
    pagination_mode = "page"
    page_param = "page"
    limit_param = "limit"
    page_size = 100

    def next_cursor(self, raw: dict[str, Any]):
        return self._cursor(raw)

    async def execute(self, operation: str, args: dict[str, Any], *, idempotency_key: Optional[str] = None) -> ConnectorResult:
        spec = self.action_specs.get(operation)
        if not spec:
            raise ConnectorError(f"{self.provider} live operation {operation} is not implemented")
        method, path, body_builder = spec
        body = body_builder(args) if callable(body_builder) else body_builder
        headers = self.headers()
        if idempotency_key:
            headers.update(self.idempotency_headers(idempotency_key))
        formatted_path = self._format_path(path, args)
        raw = await self.request(method, self._url(formatted_path), headers={**headers, "Content-Type": "application/json"}, json_body=body, safe_to_retry=bool(idempotency_key and self.supports_idempotency))
        external_id = self._first(raw, "id", "sid", "uri", "resource.id") if isinstance(raw, dict) else None
        return ConnectorResult(self.provider, operation, [], external_id=str(external_id) if external_id else None, raw=raw if isinstance(raw, dict) else {"data": raw})

    def idempotency_headers(self, key: str):
        return {"Idempotency-Key": key}

    action_specs: dict[str, Any] = {}


class BearerRESTAdapter(FabricAdapter):
    """Fixed REST provider with standard bearer auth and explicit resource paths."""


class MicrosoftGraphAdapter(FabricAdapter):
    provider = "microsoft_graph"; base = "https://graph.microsoft.com/v1.0"; health_path = "/me"
    resource_paths = {"contacts": ("/me/contacts", ("value",)), "messages": ("/me/messages", ("value",)), "events": ("/me/events", ("value",))}
    page_size = 100
    action_specs = {"send.email": ("POST", "/me/sendMail", lambda a: {"message": {"subject": a["subject"], "body": {"contentType": "Text", "content": a["body"]}, "toRecipients": [{"emailAddress": {"address": a["to"]}}]}, "saveToSentItems": True})}

    async def sync(self, resource, cursor=None):
        if resource not in self.resource_paths:
            raise ConnectorError(f"Microsoft Graph resource {resource} is not implemented")
        url = cursor if cursor and str(cursor).startswith("https://") else self._url(self.resource_paths[resource][0])
        params = None if url != self._url(self.resource_paths[resource][0]) else {"$top": self.page_size}
        raw = await self.request("GET", url, headers=self.headers(), params=params)
        records = [e for x in self._items(raw, "value") if (e := self.normalize(resource, x))]
        return ConnectorResult(self.provider, f"sync.{resource}", records, next_cursor=raw.get("@odata.nextLink"), raw=raw)


class SalesforceAdapter(FabricAdapter):
    provider = "salesforce"; api_version = "v60.0"
    resource_paths = {"contacts": (), "leads": (), "opportunities": ()}
    def _base(self):
        base = self.credentials.get("instance_url") or self.config.get("base_url")
        if not base: raise ConnectorError("Salesforce requires instance_url")
        return base.rstrip("/")
    def headers(self):
        return {"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
    async def sync(self, resource, cursor=None):
        soql = {
            "contacts":"SELECT Id,Name,Email,Phone,LastModifiedDate FROM Contact ORDER BY LastModifiedDate,Id",
            "leads":"SELECT Id,Name,Email,Phone,Status,Company,LastModifiedDate FROM Lead ORDER BY LastModifiedDate,Id",
            "opportunities":"SELECT Id,Name,Amount,StageName,LastModifiedDate FROM Opportunity ORDER BY LastModifiedDate,Id",
        }.get(resource)
        if not soql: raise ConnectorError(f"Salesforce resource {resource} is not implemented")
        url = cursor if cursor and str(cursor).startswith("https://") else self._url(f"/services/data/{self.api_version}/query")
        params = None if url != self._url(f"/services/data/{self.api_version}/query") else {"q": soql}
        raw = await self.request("GET", url, headers=self.headers(), params=params)
        records=[]
        for x in raw.get("records", []):
            event=self.normalize(resource, x)
            if event: records.append(event)
        nxt = raw.get("nextRecordsUrl")
        return ConnectorResult(self.provider, f"sync.{resource}", records, next_cursor=(self._url(nxt) if nxt else None), raw=raw)
    action_specs = {"create.contact": ("POST", "/services/data/v60.0/sobjects/Contact", lambda a: {k:v for k,v in {"FirstName":a.get("firstname"),"LastName":a.get("lastname") or "Customer","Email":a.get("email"),"Phone":a.get("phone")}.items() if v})}


class PipedriveAdapter(FabricAdapter):
    provider="pipedrive"; base="https://api.pipedrive.com/api/v2"; health_path="/users/me"
    resource_paths={"persons":("/persons",("data",)),"deals":("/deals",("data",)),"activities":("/activities",("data",))}
    action_specs={"create.activity":("POST","/activities",lambda a:{k:v for k,v in {"subject":a.get("subject"),"type":a.get("type","task"),"due_date":a.get("due_date"),"due_time":a.get("due_time"),"person_id":a.get("person_id"),"deal_id":a.get("deal_id"),"note":a.get("note")}.items() if v is not None})}
    def headers(self):
        token=self.token()
        if not token: raise ConnectorError("Pipedrive requires OAuth access token")
        return {"Authorization": f"Bearer {token}", "Accept":"application/json"}


class ZohoCRMAdapter(FabricAdapter):
    provider="zoho_crm"; base="https://www.zohoapis.com/crm/v8"; health_path="/users?type=CurrentUser"
    resource_paths={"leads":("/Leads",("data",)),"contacts":("/Contacts",("data",)),"deals":("/Deals",("data",)),"invoices":("/Invoices",("data",))}
    action_specs={"create.contact":("POST","/Contacts",lambda a:{"data":[{k:v for k,v in {"First_Name":a.get("firstname"),"Last_Name":a.get("lastname") or "Customer","Email":a.get("email"),"Phone":a.get("phone")}.items() if v}]})}
    def headers(self):
        t=self.token(); return {"Authorization":f"Zoho-oauthtoken {t}","Accept":"application/json"}


class QuickBooksAdapter(FabricAdapter):
    provider="quickbooks"; base="https://quickbooks.api.intuit.com/v3/company"; health_path="/"
    resource_paths = {"customers": (), "invoices": (), "payments": ()}
    async def health(self):
        realm=self.credentials.get("realm_id") or self.config.get("realm_id")
        if not realm: raise ConnectorError("QuickBooks requires realm_id")
        query="select * from CompanyInfo maxresults 1"
        await self.request("GET", f"{self._base()}/{quote(str(realm))}/query", headers=self.headers(), params={"query":query})
        return {"ok":True}
    async def sync(self, resource, cursor=None):
        realm=self.credentials.get("realm_id") or self.config.get("realm_id")
        if not realm: raise ConnectorError("QuickBooks requires realm_id")
        entity={"customers":"Customer","invoices":"Invoice","payments":"Payment"}.get(resource)
        if not entity: raise ConnectorError(f"QuickBooks resource {resource} is not implemented")
        page=max(1,int(cursor or 1)); page_size=min(1000,max(1,int(self.config.get("page_size",100))))
        start_pos=(page-1)*page_size+1
        query=f"select * from {entity} startposition {start_pos} maxresults {page_size}"
        raw=await self.request("GET",f"{self._base()}/{quote(str(realm))}/query",headers=self.headers(),params={"query":query})
        items=self._items(raw,entity)
        records=[e for x in items if (e:=self.normalize(resource,x))]
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=str(page+1) if len(items)>=page_size else None,raw=raw)


class XeroAdapter(FabricAdapter):
    provider="xero"; base="https://api.xero.com/api.xro/2.0"; health_path="/Organisation"
    resource_paths={"contacts":("/Contacts",("Contacts",)),"invoices":("/Invoices",("Invoices",)),"payments":("/Payments",("Payments",))}
    def headers(self):
        h=super().headers(); tid=self.credentials.get("tenant_id") or self.config.get("tenant_id")
        if not tid: raise ConnectorError("Xero requires tenant_id")
        h["Xero-tenant-id"]=tid; h["Accept"]="application/json"; return h
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"Xero resource {resource} is not implemented")
        page=max(1,int(cursor or 1)); raw=await self.request("GET",self._url(spec[0]),headers=self.headers(),params={"page":page})
        items=self._items(raw,*spec[1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=str(page+1) if len(items)>=100 else None,raw=raw)


class SquareAdapter(FabricAdapter):
    provider="square"; base="https://connect.squareup.com/v2"; health_path="/merchants/me"
    resource_paths={"customers":("/customers",("customers",)),"payments":("/payments",("payments",)),"orders":("/orders/search",("orders",))}
    def headers(self):
        h=super().headers(); h["Square-Version"]=self.credentials.get("square_version","2026-09-16"); return h
    async def sync(self,resource,cursor=None):
        if resource=="orders":
            body={"limit":100}
            if cursor: body["cursor"]=cursor
            raw=await self.request("POST",self._url("/orders/search"),headers={**self.headers(),"Content-Type":"application/json"},json_body=body)
        else:
            path=self.resource_paths[resource][0]; params={"limit":100}
            if cursor: params["cursor"]=cursor
            raw=await self.request("GET",self._url(path),headers=self.headers(),params=params)
        items=self._items(raw,*self.resource_paths[resource][1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=raw.get("cursor"),raw=raw)
    action_specs={"create.customer":("POST","/customers",lambda a:{k:v for k,v in {"given_name":a.get("firstname"),"family_name":a.get("lastname"),"email_address":a.get("email"),"phone_number":a.get("phone")}.items() if v})}


class ShopifyAdapter(FabricAdapter):
    supports_idempotency = True
    provider="shopify"; api_version="2026-10"
    def _base(self):
        shop=self.credentials.get("shop_domain") or self.config.get("shop_domain")
        if not shop: raise ConnectorError("Shopify requires shop_domain")
        return "https://"+shop.rstrip("/")+f"/admin/api/{self.api_version}"
    health_path="/shop.json"
    def headers(self):
        token=self.token()
        if not token: raise ConnectorError("Shopify requires access_token")
        return {"X-Shopify-Access-Token":token,"Accept":"application/json"}
    resource_paths={"customers":("/customers.json",("customers",)),"orders":("/orders.json",("orders",)),"products":("/products.json",("products",))}
    def _next_link(self):
        link=self.last_response_headers.get("link") or self.last_response_headers.get("Link".lower())
        if not link: return None
        for part in link.split(","):
            if '; rel="next"' in part or '; rel=next' in part:
                m=re.search(r'<([^>]+)>', part)
                return m.group(1) if m else None
        return None
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"Shopify resource {resource} is not implemented")
        if cursor and str(cursor).startswith("https://"):
            url=str(cursor); params=None
        else:
            url=self._url(spec[0]); params={"limit":min(250,max(1,int(self.config.get("page_size",250))))}
        raw=await self.request("GET",url,headers=self.headers(),params=params)
        items=self._items(raw,*spec[1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=self._next_link(),raw=raw)
    action_specs={"create.customer":("POST","/customers.json",lambda a:{"customer":{k:v for k,v in {"first_name":a.get("firstname"),"last_name":a.get("lastname"),"email":a.get("email"),"phone":a.get("phone")}.items() if v}})}


class WooCommerceAdapter(FabricAdapter):
    provider="woocommerce"; health_path="/wp-json/wc/v3/system_status"
    def _base(self):
        base=self.credentials.get("store_url") or self.config.get("store_url") or self.credentials.get("base_url")
        if not base: raise ConnectorError("WooCommerce requires store_url")
        return base.rstrip("/")
    def headers(self):
        key=self.credentials.get("consumer_key"); secret=self.credentials.get("consumer_secret")
        if not key or not secret: raise ConnectorError("WooCommerce requires consumer_key and consumer_secret")
        raw=base64.b64encode(f"{key}:{secret}".encode()).decode()
        return {"Authorization":f"Basic {raw}","Accept":"application/json"}
    resource_paths={"customers":("/wp-json/wc/v3/customers",()),"orders":("/wp-json/wc/v3/orders",()),"products":("/wp-json/wc/v3/products",())}
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"WooCommerce resource {resource} is not implemented")
        page=int(cursor) if cursor else 1
        raw=await self.request("GET",self._url(spec[0]),headers=self.headers(),params={"page":page,"per_page":100})
        items=self._items(raw)
        records=[e for x in items if (e:=self.normalize(resource,x))]
        total_pages=int(self.last_response_headers.get("x-wp-totalpages","1") or "1")
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=str(page+1) if page < total_pages else None,raw={"body":raw,"total_pages":total_pages})


class CalendlyAdapter(FabricAdapter):
    provider="calendly"; base="https://api.calendly.com"; health_path="/users/me"
    resource_paths={"scheduled_events":("/scheduled_events",("collection",)),"invitees":("/scheduled_events/{event_uuid}/invitees",("collection",))}
    def headers(self): return {"Authorization":f"Bearer {self.token()}","Accept":"application/json"}
    async def sync(self,resource,cursor=None):
        if resource == "scheduled_events":
            url = cursor if cursor and str(cursor).startswith("https://") else self._url("/scheduled_events")
            params=None if url != self._url("/scheduled_events") else {"count":100}
        elif resource == "invitees":
            event_uuid=self.config.get("event_uuid") or self.credentials.get("event_uuid")
            if not event_uuid: raise ConnectorError("Calendly invitees sync requires event_uuid")
            url=cursor if cursor and str(cursor).startswith("https://") else self._url(f"/scheduled_events/{quote(str(event_uuid),safe='')}/invitees")
            params=None if url != self._url(f"/scheduled_events/{quote(str(event_uuid),safe='')}/invitees") else {"count":100}
        else: raise ConnectorError(f"Calendly resource {resource} is not implemented")
        if params is not None and cursor: params["page_token"]=cursor
        raw=await self.request("GET",url,headers=self.headers(),params=params)
        records=[e for x in self._items(raw,"collection") if (e:=self.normalize(resource,x))]
        page=(raw.get("pagination") or {})
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=page.get("next_page") or page.get("next_page_token"),raw=raw)


class SlackAdapter(FabricAdapter):
    provider="slack"; base="https://slack.com/api"; health_path="/auth.test"
    resource_paths={"messages":("/conversations.history",("messages",)),"channels":("/conversations.list",("channels",)),"users":("/users.list",("members",))}
    action_specs={"send.message":("POST","/chat.postMessage",lambda a:{"channel":a["channel"],"text":a["body"]})}
    def headers(self): return {"Authorization":f"Bearer {self.token()}","Content-Type":"application/json"}
    async def sync(self,resource,cursor=None):
        if resource == "messages":
            channel=self.config.get("channel_id") or self.credentials.get("channel_id")
            if not channel: raise ConnectorError("Slack messages sync requires channel_id")
            params={"channel":channel,"limit":200}
            if cursor: params["cursor"]=cursor
            raw=await self.request("GET",self._url("/conversations.history"),headers=self.headers(),params=params)
        elif resource == "channels":
            params={"limit":200}
            if cursor: params["cursor"]=cursor
            raw=await self.request("GET",self._url("/conversations.list"),headers=self.headers(),params=params)
        elif resource == "users":
            params={"limit":200}
            if cursor: params["cursor"]=cursor
            raw=await self.request("GET",self._url("/users.list"),headers=self.headers(),params=params)
        else: raise ConnectorError(f"Slack resource {resource} is not implemented")
        items=self._items(raw,*self.resource_paths[resource][1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        nxt=(raw.get("response_metadata") or {}).get("next_cursor") or self._cursor(raw)
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=nxt,raw=raw)


class MicrosoftTeamsAdapter(MicrosoftGraphAdapter):
    provider="microsoft_teams"
    resource_paths={"messages":("/teams/{team_id}/channels/{channel_id}/messages",("value",))}
    def resource_request(self,resource,cursor):
        if resource!="messages": raise ConnectorError("Teams supports messages resource; configure team_id/channel_id")
        team=self.config.get("team_id") or self.credentials.get("team_id"); channel=self.config.get("channel_id") or self.credentials.get("channel_id")
        if not team or not channel: raise ConnectorError("Teams requires team_id and channel_id")
        if cursor and str(cursor).startswith("https://"):
            return str(cursor), {}, ("value",)
        return f"/teams/{team}/channels/{channel}/messages", {"$top":100}, ("value",)
    action_specs={"send.message":("POST","/teams/{team_id}/channels/{channel_id}/messages",lambda a:{"body":{"content":a["body"]}})}
    def execute(self,operation,args,*,idempotency_key=None):
        args={**args,"team_id":args.get("team_id") or self.config.get("team_id") or self.credentials.get("team_id"),"channel_id":args.get("channel_id") or self.config.get("channel_id") or self.credentials.get("channel_id")}
        if not args["team_id"] or not args["channel_id"]: raise ConnectorError("Teams requires team_id and channel_id")
        return super().execute(operation,args,idempotency_key=idempotency_key)


class ServiceTitanAdapter(FabricAdapter):
    provider="servicetitan"
    resource_paths = {"customers": (), "jobs": (), "appointments": (), "estimates": (), "invoices": (), "payments": ()}
    async def health(self):
        tenant=self.credentials.get("tenant_id") or self.config.get("tenant_id")
        if not tenant: raise ConnectorError("ServiceTitan requires tenant_id")
        await self.request("GET", self._url(f"/settings/v2/tenant/{tenant}/employees"), headers=self.headers(), params={"pageSize":1})
        return {"ok":True}
    def _base(self):
        tenant=self.credentials.get("tenant_id") or self.config.get("tenant_id")
        if not tenant: raise ConnectorError("ServiceTitan requires tenant_id")
        env=self.config.get("environment","integration")
        host="api.servicetitan.io" if env=="production" else "api-integration.servicetitan.io"
        return f"https://{host}"
    def headers(self):
        token=self.token(); key=self.credentials.get("app_key") or self.config.get("app_key")
        if not token or not key: raise ConnectorError("ServiceTitan requires OAuth access_token and app_key")
        return {"Authorization":f"Bearer {token}","ST-App-Key":key,"Accept":"application/json"}
    async def sync(self,resource,cursor=None):
        tenant=self.credentials.get("tenant_id") or self.config.get("tenant_id")
        paths={"customers":f"/crm/v2/tenant/{tenant}/customers","jobs":f"/jpm/v2/tenant/{tenant}/jobs","appointments":f"/jpm/v2/tenant/{tenant}/appointments","invoices":f"/accounting/v2/tenant/{tenant}/invoices","payments":f"/accounting/v2/tenant/{tenant}/payments","estimates":f"/sales/v2/tenant/{tenant}/estimates"}
        path=paths.get(resource)
        if not path: raise ConnectorError(f"ServiceTitan resource {resource} is not implemented")
        page=max(1,int(cursor or 1)); params={"pageSize":min(1000,max(1,int(self.config.get("page_size",100)))),"page":page}
        raw=await self.request("GET",self._url(path),headers=self.headers(),params=params)
        items=self._items(raw,"data","customers","jobs","appointments","invoices","payments","estimates")
        records=[e for x in items if (e:=self.normalize(resource,x))]
        nxt=str(page+1) if bool(raw.get("hasMore")) else None
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=nxt,raw=raw)
    action_specs={"update.job":("PATCH","/jpm/v2/tenant/{tenant}/jobs/{id}",lambda a:{k:v for k,v in a.items() if k in {"customerId","locationId","businessUnitId","jobTypeId","priority","summary","customerPo","soldById","noCharge","summaryOfWork"}})}
    async def execute(self,operation,args,*,idempotency_key=None):
        tenant=args.get("tenant_id") or self.credentials.get("tenant_id") or self.config.get("tenant_id"); args={**args,"tenant":tenant}
        return await super().execute(operation,args,idempotency_key=idempotency_key)


class HousecallProAdapter(FabricAdapter):
    provider="housecall_pro"; base="https://api.housecallpro.com"; health_path="/customers"
    resource_paths={"customers":("/customers",("customers",)),"jobs":("/jobs",("jobs",)),"appointments":("/appointments",("appointments",)),"invoices":("/invoices",("invoices",)),"payments":("/payments",("payments",))}
    action_specs={"create.customer":("POST","/customers",lambda a:{k:v for k,v in {"first_name":a.get("firstname"),"last_name":a.get("lastname"),"email":a.get("email"),"mobile_number":a.get("phone")}.items() if v})}
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"Housecall Pro resource {resource} is not implemented")
        page=max(1,int(cursor or 1)); page_size=min(100,max(1,int(self.config.get("page_size",100))))
        raw=await self.request("GET",self._url(spec[0]),headers=self.headers(),params={"page":page,"page_size":page_size})
        items=self._items(raw,*spec[1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        meta=raw.get("meta") or raw.get("pagination") or {}
        total_pages=meta.get("total_pages") or meta.get("totalPages")
        next_page=bool(total_pages and page < int(total_pages)) or len(items)>=page_size
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=str(page+1) if next_page else None,raw=raw)


class JobberAdapter(FabricAdapter):
    provider="jobber"; base="https://api.getjobber.com/api/graphql"; health_path=""
    resource_paths = {"clients": (), "jobs": (), "appointments": (), "invoices": ()}
    def headers(self): return {"Authorization":f"Bearer {self.token()}","X-JOBBER-GRAPHQL-VERSION":self.config.get("graphql_version","2025-04-16"),"Content-Type":"application/json"}
    async def request_graphql(self,query,variables=None): return await self.request("POST",self._base(),headers=self.headers(),json_body={"query":query,"variables":variables or {}},safe_to_retry=True)
    async def health(self): return await self.request_graphql("query { account { id name } }")
    async def sync(self,resource,cursor=None):
        roots={"clients":("clients", "id name firstName lastName emails { address } phones { number }"),"jobs":("jobs","id title startAt endAt"),"appointments":("visits","id title startAt endAt"),"invoices":("invoices","id subject totalAmount { amount }")}
        spec=roots.get(resource)
        if not spec: raise ConnectorError(f"Jobber resource {resource} is not implemented")
        root, fields=spec
        q=f"query($after:String) {{ {root}(first:100, after:$after) {{ nodes {{ {fields} }} pageInfo {{ hasNextPage endCursor }} }} }}"
        raw=await self.request_graphql(q,{"after":cursor})
        if raw.get("errors"):
            raise ConnectorError("Jobber GraphQL request returned a schema/API error")
        data=raw.get("data") or {}
        obj=data.get(root) or {}
        items=obj.get("nodes") or []
        records=[e for x in items if (e:=self.normalize(resource,x))]
        pi=obj.get("pageInfo") or {}
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=pi.get("endCursor") if pi.get("hasNextPage") else None,raw=raw)


class JobNimbusAdapter(FabricAdapter):
    provider="jobnimbus"; base="https://api.jobnimbus.com"; health_path="/identity/v1/me/companies"
    resource_paths={
        "contacts":("/contacts/v1/contacts",("data","contacts","items")),
        "jobs":("/jobs/v1/jobs",("data","jobs","items")),
        "tasks":("/tasks/v1/tasks",("data","tasks","items")),
    }
    def headers(self):
        token=self.token()
        if not token: raise ConnectorError("JobNimbus requires API key")
        return {"Authorization":f"Bearer {token}","Content-Type":"application/json","Accept":"application/json"}
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"JobNimbus resource {resource} is not implemented")
        params={"pageSize":min(200,max(1,int(self.config.get("page_size",100))))}
        if cursor: params["cursor"]=cursor
        raw=await self.request("GET",self._url(spec[0]),headers=self.headers(),params=params)
        items=self._items(raw,*spec[1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        nxt=(raw.get("nextCursor") or (raw.get("pagination") or {}).get("nextCursor") or (raw.get("pageInfo") or {}).get("endCursor"))
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=str(nxt) if nxt else None,raw=raw)


class FieldEdgeAdapter(FabricAdapter):
    provider="fieldedge"; base="https://api.fieldedge.com/open-api"; health_path="/v1/business-units?limit=1"
    resource_paths={"customers":("/v1/customers",("data","customers")),"leads":("/v1/leads",("data","leads")),"work_orders":("/v1/work-orders",("data","workOrders")),"appointments":("/v1/appointments",("data","appointments")),"invoices":("/v1/invoices",("data","invoices")),"payments":("/v1/payments",("data","payments"))}
    action_specs={"create.lead":("POST","/v1/leads",lambda a:{"source":a.get("source","RROS"),"preferredContactMethod":a.get("preferredContactMethod","phone"),"isExistingCustomer":bool(a.get("existing_customer_id")),"customer":{k:v for k,v in {"existingCustomerId":a.get("existing_customer_id"),"firstName":a.get("firstname"),"lastName":a.get("lastname"),"email":a.get("email"),"phone":a.get("phone")}.items() if v}})}


class AccuLynxAdapter(FabricAdapter):
    provider="acculynx"; base="https://api.acculynx.com"; health_path="/api/v2/contacts?pageSize=1&pageStartIndex=0"
    resource_paths={"companies":("/api/v2/companies",("companies","data")),"jobs":("/api/v2/jobs",("jobs","data")),"estimates":("/api/v2/estimates",("estimates","data")),"invoices":("/api/v2/invoices",("invoices","data"))}
    def headers(self):
        token=self.token()
        if not token: raise ConnectorError("AccuLynx requires API key")
        return {"Authorization":f"Bearer {token}","Accept":"application/json"}
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"AccuLynx resource {resource} is not implemented")
        page_size=min(100,max(1,int(self.config.get("page_size",100)))); start_index=max(0,int(cursor or 0))
        raw=await self.request("GET",self._url(spec[0]),headers=self.headers(),params={"pageSize":page_size,"pageStartIndex":start_index})
        items=self._items(raw,*spec[1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        # The API uses record indices and returns HTTP 416 past the end. A full page is the safe continuation signal.
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=str(start_index+len(items)) if len(items)>=page_size else None,raw=raw)


class GorillaDeskAdapter(FabricAdapter):
    provider="gorilladesk"; base="https://api.gorilladesk.com"; health_path="/v1/customers"
    resource_paths={"customers":("/v1/customers",("data","customers")),"jobs":("/v1/jobs",("data","jobs")),"appointments":("/v1/jobs",("data","jobs")),"invoices":("/v1/invoices",("data","invoices")),"payments":("/v1/payments",("data","payments"))}
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"GorillaDesk resource {resource} is not implemented")
        limit=min(100,max(1,int(self.config.get("page_size",100)))); offset=max(0,int(cursor or 0))
        raw=await self.request("GET",self._url(spec[0]),headers=self.headers(),params={"limit":limit,"offset":offset})
        items=self._items(raw,*spec[1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        meta=raw.get("meta") or raw.get("pagination") or {}
        has_more=bool(raw.get("has_more") or raw.get("hasMore") or meta.get("has_more") or meta.get("hasMore") or len(items)>=limit)
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=str(offset+len(items)) if has_more and items else None,raw=raw)


class Dynamics365Adapter(FabricAdapter):
    provider="dynamics_365"; health_path="/api/data/v9.2/WhoAmI"
    def _base(self):
        base=self.credentials.get("instance_url") or self.config.get("base_url")
        if not base: raise ConnectorError("Dynamics 365 requires instance_url")
        return base.rstrip("/")
    resource_paths={"contacts":("/api/data/v9.2/contacts",("value",)),"leads":("/api/data/v9.2/leads",("value",)),"opportunities":("/api/data/v9.2/opportunities",("value",))}
    action_specs={"create.contact":("POST","/api/data/v9.2/contacts",lambda a:{k:v for k,v in {"firstname":a.get("firstname"),"lastname":a.get("lastname") or "Customer","emailaddress1":a.get("email"),"telephone1":a.get("phone")}.items() if v})}


class FreshsalesAdapter(FabricAdapter):
    provider="freshsales"
    health_path="/crm/sales/api/contacts?per_page=1"

    def _base(self):
        domain=self.credentials.get("domain") or self.config.get("domain") or self.credentials.get("base_url") or self.config.get("base_url")
        if not domain:
            raise ConnectorError("Freshsales requires account domain/base_url")
        value=("https://"+str(domain) if not str(domain).startswith("https://") else str(domain)).rstrip("/")
        # Normalize domains that were pasted with the API prefix.
        value=re.sub(r"/crm/sales(?:/api)?$", "", value)
        return value

    def headers(self):
        token=self.credentials.get("api_key") or self.credentials.get("access_token") or self.token()
        if not token:
            raise ConnectorError("Freshsales requires api_key")
        return {"Authorization": f"Token token={token}", "Accept":"application/json"}

    resource_paths={
        "contacts":("/crm/sales/api/contacts",("contacts",)),
        "deals":("/crm/sales/api/deals",("deals",)),
        "activities":("/crm/sales/api/activities",("activities",)),
    }

    async def sync(self, resource, cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec:
            raise ConnectorError(f"Freshsales resource {resource} is not implemented")
        page=max(1, int(cursor or 1))
        raw=await self.request("GET", self._url(spec[0]), headers=self.headers(), params={"page":page,"per_page":100})
        items=self._items(raw,*spec[1])
        records=[e for x in items if (e:=self.normalize(resource,x))]
        pages=raw.get("meta") if isinstance(raw,dict) else None
        if not isinstance(pages,dict):
            pages=raw.get("pagination") if isinstance(raw,dict) else None
        next_cursor=None
        if isinstance(pages,dict):
            nxt=pages.get("next_page") or pages.get("next") or pages.get("next_page_number")
            if isinstance(nxt,int) and nxt > page:
                next_cursor=str(nxt)
            elif isinstance(nxt,str) and nxt.isdigit() and int(nxt)>page:
                next_cursor=nxt
        if not next_cursor and len(items) >= 100:
            next_cursor=str(page+1)
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=next_cursor,raw=raw)


class CloseAdapter(FabricAdapter):
    provider="close"; base="https://api.close.com/api/v1"; health_path="/me/"
    resource_paths={"leads":("/lead/",("data",)),"contacts":("/contact/",("data",)),"activities":("/activity/",("data",)),"opportunities":("/opportunity/",("data",))}
    def headers(self):
        token=self.token()
        if not token: raise ConnectorError("Close requires api_key")
        basic=base64.b64encode(f"{token}:".encode()).decode(); return {"Authorization":f"Basic {basic}","Accept":"application/json"}


class KeapAdapter(FabricAdapter):
    provider="keap"; base="https://api.infusionsoft.com/crm/rest/v2"; health_path="/account/profile"
    resource_paths={"contacts":("/contacts",("contacts",)),"opportunities":("/opportunities",("opportunities",)),"orders":("/orders",("orders",)),"appointments":("/appointments",("appointments",))}


class ZendeskAdapter(FabricAdapter):
    provider="zendesk"
    def _base(self):
        sub=self.credentials.get("subdomain") or self.config.get("subdomain")
        base=self.credentials.get("base_url") or self.config.get("base_url")
        if not base and sub: base=f"https://{sub}.zendesk.com"
        if not base: raise ConnectorError("Zendesk requires subdomain or base_url")
        return base.rstrip("/")
    resource_paths={"tickets":("/api/v2/tickets.json",("tickets",)),"users":("/api/v2/users.json",("users",)),"organizations":("/api/v2/organizations.json",("organizations",))}
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"Zendesk resource {resource} is not implemented")
        url=cursor if cursor and str(cursor).startswith("https://") else self._url(spec[0])
        params=None if url != self._url(spec[0]) else {"page[size]":100}
        if params and cursor: params["page[after]"]=cursor
        raw=await self.request("GET",url,headers=self.headers(),params=params)
        items=self._items(raw,*spec[1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        nxt=(raw.get("meta") or {}).get("after_cursor")
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=nxt,raw=raw)


class IntercomAdapter(FabricAdapter):
    provider="intercom"; base="https://api.intercom.io"; health_path="/me"
    resource_paths={"contacts":("/contacts",("data",)),"conversations":("/conversations",("conversations",)),"tickets":("/tickets",("tickets",))}
    def headers(self): return {"Authorization":f"Bearer {self.token()}","Accept":"application/json","Intercom-Version":self.credentials.get("intercom_version") or self.config.get("intercom_version","2.16")}
    action_specs={"reply.conversation":("POST","/conversations/{conversation_id}/reply",lambda a:{"message_type":"comment","type":"admin","body":a["body"]})}
    async def sync(self,resource,cursor=None):
        spec=self.resource_paths.get(resource)
        if not spec: raise ConnectorError(f"Intercom resource {resource} is not implemented")
        params={"per_page":150}
        if cursor: params["starting_after"]=cursor
        raw=await self.request("GET",self._url(spec[0]),headers=self.headers(),params=params)
        items=self._items(raw,*spec[1]); records=[e for x in items if (e:=self.normalize(resource,x))]
        nxt=((raw.get("pages") or {}).get("next") or {}).get("starting_after")
        if not nxt:
            # Some resources expose the cursor in pages.next only.
            nxt=((raw.get("pages") or {}).get("next") or {}).get("starting_after")
        return ConnectorResult(self.provider,f"sync.{resource}",records,next_cursor=nxt,raw=raw)


class YelpAdapter(FabricAdapter):
    provider="yelp"; base="https://api.yelp.com/v3"; health_path="/businesses/search?term=business&location=San%20Francisco&limit=1"
    resource_paths={"reviews":("/businesses/{business_id}/reviews",("reviews",)),"businesses":("/businesses/search",("businesses",))}
    def resource_request(self,resource,cursor):
        if resource=="reviews":
            bid=self.credentials.get("business_id") or self.config.get("business_id")
            if not bid: raise ConnectorError("Yelp reviews require business_id")
            return f"/businesses/{quote(str(bid),safe='')}/reviews",{},("reviews",)
        params={"term":self.config.get("term","business"),"location":self.config.get("location","San Francisco"),"limit":50}
        return "/businesses/search",params,("businesses",)
    def headers(self): return {"Authorization":f"Bearer {self.token()}","Accept":"application/json"}


# Register every catalog provider with an executable adapter. Existing mature adapters
# remain preferred; these adapters cover the remaining providers and can be overridden
# with provider-specific subclasses later without changing the orchestration contract.
FABRIC_ADAPTERS = {
    "microsoft_graph": MicrosoftGraphAdapter, "salesforce": SalesforceAdapter,
    "pipedrive": PipedriveAdapter, "zoho_crm": ZohoCRMAdapter, "quickbooks": QuickBooksAdapter,
    "xero": XeroAdapter, "square": SquareAdapter, "shopify": ShopifyAdapter,
    "woocommerce": WooCommerceAdapter, "calendly": CalendlyAdapter, "slack": SlackAdapter,
    "microsoft_teams": MicrosoftTeamsAdapter, "servicetitan": ServiceTitanAdapter,
    "housecall_pro": HousecallProAdapter, "jobber": JobberAdapter, "jobnimbus": JobNimbusAdapter,
    "fieldedge": FieldEdgeAdapter, "acculynx": AccuLynxAdapter, "gorilladesk": GorillaDeskAdapter,
    "dynamics_365": Dynamics365Adapter, "freshsales": FreshsalesAdapter, "close": CloseAdapter,
    "keap": KeapAdapter, "zendesk": ZendeskAdapter, "intercom": IntercomAdapter, "yelp": YelpAdapter,
}

# FieldEdge has no stable public endpoint documented in the current catalog; it is
# executable only when the account supplies its approved HTTPS API base/resource paths.
FABRIC_ADAPTERS["fieldedge"] = FieldEdgeAdapter

# Keep the complete registry visible from app.connectors when this module is imported first.
from . import connectors as _connectors
_connectors.ADAPTERS.update(FABRIC_ADAPTERS)
