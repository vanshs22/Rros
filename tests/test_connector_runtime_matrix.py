import asyncio
import pytest

from app.connectors import ADAPTERS, adapter_for
from app.integrations import PROVIDERS
from app.runtime import SYNC_RESOURCES, adapter_contract
from app.connector_core import ConnectorResult


def _creds(provider):
    base = {"access_token": "test-token", "api_key": "test-key", "token": "test-token", "auth_token": "test-auth"}
    extras = {
        "twilio": {"account_sid": "ACtest", "auth_token": "test-auth", "from_number": "+15550000000"},
        "whatsapp_twilio": {"account_sid": "ACtest", "auth_token": "test-auth", "whatsapp_from": "whatsapp:+15550000000"},
        "gohighlevel": {"location_id": "loc-test", "start_time_ms": 0, "end_time_ms": 9999999999999},
        "salesforce": {"instance_url": "https://example.my.salesforce.com"},
        "quickbooks": {"realm_id": "realm-test"},
        "xero": {"tenant_id": "xero-test"},
        "dynamics_365": {"instance_url": "https://example.crm.dynamics.com"},
        "freshsales": {"domain": "example.myfreshworks.com", "api_key": "test-key"},
        "zendesk": {"subdomain": "example"},
        "yelp": {"business_id": "biz-test"},
        "fieldedge": {"target_id": "target-test"},
        "shopify": {"shop_domain": "test-shop.myshopify.com"},
        "slack": {"channel_id": "C123"},
        "microsoft_teams": {"team_id": "T123", "channel_id": "C123"},
        "woocommerce": {"store_url": "https://shop.example.com", "consumer_key": "ck_test", "consumer_secret": "cs_test"},
        "jobber": {"access_token": "test-token"},
        "servicetitan": {"tenant_id": "tenant-test", "app_key": "app-test"},
    }
    base.update(extras.get(provider, {}))
    return base


def _fake_empty(provider):
    if provider == "jobber":
        return {"data": {}}
    return {}


def test_every_catalog_provider_has_adapter_and_all_catalog_resources_are_executable():
    assert set(PROVIDERS) == set(ADAPTERS)
    for provider in sorted(PROVIDERS):
        cls = ADAPTERS[provider]
        contract = adapter_contract(cls)
        assert contract["has_sync"], provider
        missing = set(SYNC_RESOURCES.get(provider, ())) - set(contract["resources"])
        # Mature adapters declare resource_paths as metadata; the remaining explicit
        # sync adapters also expose their exact resource set now.
        assert not missing, (provider, missing)


@pytest.mark.asyncio
async def test_every_provider_resource_can_execute_through_its_adapter_boundary(monkeypatch):
    for provider, resources in sorted(SYNC_RESOURCES.items()):
        credentials = _creds(provider)
        config = dict(credentials)
        if provider == "gohighlevel":
            config.update({"start_time_ms": 1, "end_time_ms": 9999999999999, "calendar_id": "cal-test"})
        elif provider in {"slack", "microsoft_teams"}:
            config.update(_creds(provider).get("x", {}))
        adapter = adapter_for(provider, credentials, config)
        calls = []

        async def fake_request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return _fake_empty(provider)

        async def fake_graphql(query, variables):
            calls.append(("POST", "graphql", {"query": query, "variables": variables}))
            return {"data": {}}

        monkeypatch.setattr(adapter, "request", fake_request)
        if hasattr(adapter, "request_graphql"):
            monkeypatch.setattr(adapter, "request_graphql", fake_graphql)
        for resource in resources:
            result = await adapter.sync(resource, cursor=None)
            assert isinstance(result, ConnectorResult), (provider, resource)
        await adapter.close()


def _action_args(operation):
    return {
        "to": "+15555555555", "body": "test", "subject": "test", "email": "a@example.com",
        "firstname": "Test", "lastname": "User", "phone": "+15555555555",
        "contact_id": "contact-test", "conversation_id": "conversation-test",
        "summary": "Test", "start": "2026-10-02T12:00:00Z", "end": "2026-10-02T13:00:00Z",
        "review_name": "accounts/1/locations/1/reviews/1", "comment": "Thanks",
        "job_id": "job-test", "id": "job-test", "status": "completed", "due_date": "2026-10-02",
        "due_time": "12:00", "type": "task", "note": "Test", "person_id": 1,
        "deal_id": 1, "source": "RROS", "preferredContactMethod": "phone",
        "existing_customer_id": "cust-test", "existingCustomerId": "cust-test",
    }


@pytest.mark.asyncio
async def test_every_declared_action_has_a_provider_execution_boundary(monkeypatch):
    for provider, cls in sorted(ADAPTERS.items()):
        contract = adapter_contract(cls)
        if not contract["actions"]:
            continue
        credentials = _creds(provider)
        config = dict(credentials)
        adapter = adapter_for(provider, credentials, config)
        async def fake_request(method, url, **kwargs):
            return {"id": f"ext-{provider}", "sid": f"sid-{provider}"}
        async def fake_send_message(*args, **kwargs):
            return ConnectorResult(provider, "send", [], external_id=f"ext-{provider}", raw={"id": f"ext-{provider}"})
        async def fake_send_email(*args, **kwargs):
            return ConnectorResult(provider, "send.email", [], external_id=f"ext-{provider}", raw={"id": f"ext-{provider}"})
        async def fake_create_event(*args, **kwargs):
            return ConnectorResult(provider, "create_event", [], external_id=f"ext-{provider}", raw={"id": f"ext-{provider}"})
        async def fake_upsert_contact(*args, **kwargs):
            return ConnectorResult(provider, "upsert_contact", [], external_id=f"ext-{provider}", raw={"id": f"ext-{provider}"})
        async def fake_reply_review(*args, **kwargs):
            return ConnectorResult(provider, "reply_review", [], external_id=f"ext-{provider}", raw={"id": f"ext-{provider}"})
        monkeypatch.setattr(adapter, "request", fake_request)
        if hasattr(adapter, "send_message"):
            monkeypatch.setattr(adapter, "send_message", fake_send_message)
        if hasattr(adapter, "send_email"):
            monkeypatch.setattr(adapter, "send_email", fake_send_email)
        if hasattr(adapter, "create_event"):
            monkeypatch.setattr(adapter, "create_event", fake_create_event)
        if hasattr(adapter, "upsert_contact"):
            monkeypatch.setattr(adapter, "upsert_contact", fake_upsert_contact)
        if hasattr(adapter, "reply_review"):
            monkeypatch.setattr(adapter, "reply_review", fake_reply_review)
        for operation in contract["actions"]:
            if operation == "send.message":
                args = {"message": "hello", "body": "hello", "channel": "sms", "channel_id": "C123", "team_id": "T123"}
            else:
                args = _action_args(operation)
            try:
                result = await adapter.execute(operation, args, idempotency_key=f"test:{provider}:{operation}")
            except TypeError as exc:
                pytest.fail(f"{provider} action {operation} has a broken argument contract: {exc}")
            assert isinstance(result, ConnectorResult), (provider, operation)
        await adapter.close()
