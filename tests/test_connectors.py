import pytest
from app.connectors import ADAPTERS, StripeAdapter, TwilioAdapter, GoogleCalendarAdapter, HubSpotAdapter


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"x"
    def json(self): return self._payload
    @property
    def text(self): return ""


@pytest.mark.asyncio
async def test_live_adapter_registry_has_core_connectors():
    assert {"stripe", "twilio", "google_calendar", "google_gmail", "hubspot", "gohighlevel", "google_business_profile"} <= set(ADAPTERS)


@pytest.mark.asyncio
async def test_stripe_normalizes_customers_and_payments(monkeypatch):
    a = StripeAdapter({"api_key": "sk_test"})
    async def fake(*args, **kwargs):
        return {"data": [{"id": "cus_1", "name": "A", "email": "a@example.com", "phone": "+1"}], "has_more": False}
    monkeypatch.setattr(a, "request", fake)
    r = await a.sync("customers")
    assert r.records[0]["type"] == "customer.upserted"
    assert r.records[0]["external_id"] == "cus_1"
    await a.close()


@pytest.mark.asyncio
async def test_twilio_action_uses_real_message_operation(monkeypatch):
    a = TwilioAdapter({"account_sid": "AC1", "auth_token": "tok", "from_number": "+1000"})
    async def fake(*args, **kwargs): return {"sid": "SM1"}
    monkeypatch.setattr(a, "request", fake)
    r = await a.send_message("+2000", "hello")
    assert r.external_id == "SM1"
    await a.close()


@pytest.mark.asyncio
async def test_calendar_normalizes_events(monkeypatch):
    a = GoogleCalendarAdapter({"access_token": "tok"})
    async def fake(*args, **kwargs):
        return {"items": [{"id": "e1", "summary": "Visit", "start": {"dateTime": "2026-10-02T10:00:00Z"}, "end": {"dateTime": "2026-10-02T11:00:00Z"}}]}
    monkeypatch.setattr(a, "request", fake)
    r = await a.sync("events")
    assert r.records[0]["type"] == "appointment.upserted"
    assert r.records[0]["data"]["scheduled_at"].endswith("Z")
    await a.close()


@pytest.mark.asyncio
async def test_hubspot_normalizes_contact(monkeypatch):
    a = HubSpotAdapter({"access_token": "tok"})
    async def fake(*args, **kwargs):
        return {"results": [{"id": "42", "properties": {"firstname": "Ada", "lastname": "L", "email": "ada@example.com"}}]}
    monkeypatch.setattr(a, "request", fake)
    r = await a.sync("contacts")
    assert r.records[0]["data"]["name"] == "Ada L"
    assert r.records[0]["external_id"] == "42"
    await a.close()


def test_all_catalog_providers_have_executable_adapters():
    from app.integrations import PROVIDERS
    from app.connectors import ADAPTERS
    assert len(PROVIDERS) >= 30
    assert set(PROVIDERS).issubset(set(ADAPTERS))


def test_fabric_adapter_never_accepts_http_base_url():
    from app.connector_fabric import FabricAdapter
    import pytest
    adapter = FabricAdapter({"access_token":"x"},{"base_url":"http://127.0.0.1"})
    with pytest.raises(Exception):
        import asyncio; asyncio.run(adapter.health())
    import asyncio; asyncio.run(adapter.close())
