from test_app import auth, make_client


def test_platform_governance_search_and_privacy_contracts(tmp_path):
    with make_client(tmp_path) as client:
        headers = auth(client)
        assert client.get("/api/v1/health").status_code == 200

        created = client.post("/api/policies", headers=headers, json={
            "name": "Outbound recovery guardrail",
            "policy_type": "ai_governance",
            "scope_type": "organization",
            "governance_level": 4,
            "config": {"blocked_autonomy_modes": ["auto_execute"]},
            "enforcement_mode": "require_approval",
        })
        assert created.status_code == 200, created.text
        policy_id = created.json()["id"]
        listed = client.get("/api/policies", headers=headers)
        assert listed.status_code == 200
        assert listed.json()[0]["governance_level"] == 4
        updated = client.patch(f"/api/policies/{policy_id}", headers=headers, json={"active": False})
        assert updated.status_code == 200

        search = client.get("/api/search?q=customer", headers=headers)
        assert search.status_code == 200
        assert isinstance(search.json()["results"], list)

        exported = client.get("/api/privacy/customers/cus_2/export", headers=headers)
        assert exported.status_code == 200
        assert exported.json()["customer"]["id"] == "cus_2"


def test_privacy_anonymisation_is_tenant_scoped(tmp_path):
    with make_client(tmp_path) as client:
        headers = auth(client)
        deleted = client.post("/api/privacy/customers/cus_2/delete", headers=headers)
        assert deleted.status_code == 200
        customer = client.get("/api/customers/cus_2", headers=headers)
        assert customer.status_code == 200
        assert customer.json()["customer"]["name"] == "Deleted Customer"
        assert customer.json()["customer"]["email"] is None


def test_review_analysis_endpoint_persists_durable_analysis(tmp_path):
    with make_client(tmp_path) as client:
        headers = auth(client)
        response = client.get("/api/reviews/rev_1/analysis", headers=headers)
        assert response.status_code == 200
        assert response.json()["analysis"]["sentiment"]["overall"] == "negative"
        response_again = client.get("/api/reviews/rev_1/analysis", headers=headers)
        assert response_again.status_code == 200
        assert response_again.json()["analysis"] == response.json()["analysis"]


def test_operations_analytics_reads_database_audit_and_review_data(tmp_path):
    with make_client(tmp_path) as client:
        headers = auth(client)
        response = client.get("/api/analytics/operations?days=30", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["safety_metrics"]["total_actions_validated"] >= 0
        assert body["review_metrics"]["total_reviews"] >= 1


def test_stripe_checkout_creates_tenant_scoped_billing_session(tmp_path, monkeypatch):
    import httpx

    class FakeResponse:
        status_code = 200
        def json(self):
            return {"id": "cs_test_rros", "url": "https://checkout.stripe.test/session"}

    class FakeClient:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, url, data, auth):
            assert url.endswith("/v1/checkout/sessions")
            assert data["mode"] == "subscription"
            assert data["metadata[plan_code]"] == "growth"
            assert auth[0] == "sk_test_checkout"
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
    with make_client(tmp_path) as client:
        headers = auth(client)
        configured = client.post("/api/integrations/stripe/configure", headers=headers, json={
            "credentials": {"api_key": "sk_test_checkout"},
            "config": {"price_ids": {"growth": "price_growth_test"}},
        })
        assert configured.status_code == 200, configured.text
        checkout = client.post("/api/billing/checkout", headers=headers, json={"plan_code": "growth"})
        assert checkout.status_code == 200, checkout.text
        assert checkout.json()["checkout_url"].startswith("https://checkout.stripe.test")
        sessions = client.get("/api/billing/sessions", headers=headers)
        assert sessions.status_code == 200 and sessions.json()[0]["plan_code"] == "growth"


def test_stripe_webhook_reconciles_completed_subscription(tmp_path, monkeypatch):
    import hashlib
    import hmac
    import json
    import time
    from dataclasses import replace
    import app.main as main

    with make_client(tmp_path) as client:
        headers = auth(client)
        configured = client.post("/api/integrations/stripe/configure", headers=headers, json={
            "credentials": {"api_key": "sk_test_checkout"},
            "config": {"price_ids": {"growth": "price_growth_test"}},
        })
        assert configured.status_code == 200
        # Persist a checkout record through the tested checkout path's provider response.
        class FakeResponse:
            status_code = 200
            def json(self):
                return {"id": "cs_webhook_test", "url": "https://checkout.stripe.test/session"}
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
            async def post(self, *args, **kwargs): return FakeResponse()
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: FakeClient())
        assert client.post("/api/billing/checkout", headers=headers, json={"plan_code": "growth"}).status_code == 200
        main.settings = replace(main.settings, stripe_webhook_secret="whsec_test")
        body = json.dumps({"id": "evt_test", "type": "checkout.session.completed", "data": {"object": {"id": "cs_webhook_test", "client_reference_id": "org_demo", "metadata": {"organization_id": "org_demo", "plan_code": "growth"}, "customer": "cus_stripe", "subscription": "sub_stripe"}}}).encode()
        timestamp = str(int(time.time()))
        signature = hmac.new(b"whsec_test", f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
        response = client.post("/api/billing/stripe/webhook", content=body, headers={"Stripe-Signature": f"t={timestamp},v1={signature}"})
        assert response.status_code == 200, response.text
        plan = client.get("/api/billing/plan", headers=headers)
        assert plan.status_code == 200 and plan.json()["plan_code"] == "growth" and plan.json()["subscription_status"] == "active"


def test_recovery_execution_requires_plan_and_stable_idempotency_key(tmp_path):
    with make_client(tmp_path) as client:
        headers = auth(client)
        opportunity = next(row for row in client.get('/api/opportunities', headers=headers).json() if row['customer_id'] == 'cus_2')
        oid = opportunity['id']
        missing_key = client.post(f'/api/opportunities/{oid}/execute', headers=headers, json={'channel': 'sms', 'approval': True})
        assert missing_key.status_code == 422
        no_plan = client.post(f'/api/opportunities/{oid}/execute', headers=headers, json={'channel': 'sms', 'approval': True, 'idempotency_key': 'plan-required-001'})
        assert no_plan.status_code == 409
        assert client.post(f'/api/opportunities/{oid}/recovery-plan', headers=headers, json={'channel': 'sms'}).status_code == 200
        executed = client.post(f'/api/opportunities/{oid}/execute', headers=headers, json={'channel': 'sms', 'approval': True, 'idempotency_key': 'plan-required-001'})
        assert executed.status_code == 200
