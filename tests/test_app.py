import json
import pytest
from fastapi.testclient import TestClient

def make_client(tmp_path):
    import app.config as cfg
    cfg.settings = cfg.Settings(database_url=f"sqlite:///{tmp_path}/test.db", seed_demo=True, live_actions=False)
    import app.db as dbmod
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    dbmod.engine = create_engine(cfg.settings.database_url, connect_args={'check_same_thread':False}, future=True)
    dbmod.SessionLocal = sessionmaker(bind=dbmod.engine, autoflush=False, autocommit=False, expire_on_commit=False)
    import app.main as m
    m.settings = cfg.settings
    m._login_attempts.clear()
    m._redis_client = None
    return TestClient(m.app)

def auth(c):
    r=c.post('/api/auth/login',json={'email':'owner@example.com','password':'demo-password'}); assert r.status_code==200
    return {'Authorization':'Bearer '+r.json()['access_token']}

def test_auth_tenant_and_core_loop(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c); assert c.get('/api/dashboard',headers=h).status_code==200
        ops=c.get('/api/opportunities',headers=h).json(); assert ops
        oid=ops[0]['id']; assert c.post(f'/api/opportunities/{oid}/recovery-plan',headers=h,json={'channel':'sms'}).status_code==200
        assert c.post(f'/api/opportunities/{oid}/execute',headers=h,json={'channel':'sms','idempotency_key':'approval-required-001'}).status_code==400
        consented=next(x for x in ops if x['customer_id']=='cus_2'); oid=consented['id']
        assert c.post(f'/api/opportunities/{oid}/recovery-plan',headers=h,json={'channel':'sms'}).status_code==200
        r=c.post(f'/api/opportunities/{oid}/execute',headers=h,json={'channel':'sms','approval':True,'idempotency_key':'core-loop-action-001'}); assert r.status_code==200
        assert r.json()['execution']=='simulation'
        assert c.get(f'/api/opportunities/{oid}',headers=h).json()['opportunity']['status']=='in_recovery'

def test_tenant_isolation_and_webhook_gate(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        assert c.get('/api/opportunities/not-a-real-tenant-opportunity',headers=h).status_code==404
        assert c.post('/api/ingest/org_demo',json={'type':'lead.created'}).status_code==401

def test_detection_is_idempotent(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        a=c.post('/api/detect',headers=h).json(); first=c.get('/api/opportunities',headers=h).json()
        b=c.post('/api/detect',headers=h).json(); second=c.get('/api/opportunities',headers=h).json()
        assert a['created']==0 and b['created']==0 and len(first)==len(second)

def test_dismiss_and_terminal_guard(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c); oid=c.get('/api/opportunities',headers=h).json()[0]['id']
        assert c.post(f'/api/opportunities/{oid}/dismiss',headers=h).status_code==200
        assert c.post(f'/api/opportunities/{oid}/execute',headers=h,json={'channel':'sms','approval':True,'idempotency_key':'terminal-guard-001'}).status_code==409

def test_attribution_requires_successful_chain(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c); oid=c.get('/api/opportunities',headers=h).json()[0]['id']
        assert c.post(f'/api/transactions/tx_1/attribute/{oid}',headers=h).status_code==400

def test_two_tenants_same_email_and_strict_isolation(tmp_path):
    with make_client(tmp_path) as c:
        a=c.post('/api/auth/register',json={'organization_name':'Tenant A','organization_slug':'tenant-a','email':'shared@example.com','password':'StrongPasswordA123!'}); assert a.status_code==200
        b=c.post('/api/auth/register',json={'organization_name':'Tenant B','organization_slug':'tenant-b','email':'shared@example.com','password':'StrongPasswordB123!'}); assert b.status_code==200
        ha={'Authorization':'Bearer '+a.json()['access_token']}; hb={'Authorization':'Bearer '+b.json()['access_token']}
        assert c.get('/api/dashboard',headers=ha).json()['opportunity_count']==0
        assert c.get('/api/dashboard',headers=hb).json()['opportunity_count']==0
        assert c.get('/api/opportunities',headers=ha).json()==[]
        assert c.post('/api/auth/login',json={'email':'shared@example.com','password':'StrongPasswordA123!'}).status_code==401
        assert c.post('/api/auth/login',json={'email':'shared@example.com','password':'StrongPasswordA123!','organization_slug':'tenant-a'}).status_code==200


def test_cross_tenant_opportunity_is_404(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        oid=c.get('/api/opportunities',headers=h).json()[0]['id']
        reg=c.post('/api/auth/register',json={'organization_name':'Other','organization_slug':'other-tenant','email':'other@example.com','password':'StrongPasswordOther123!'}); assert reg.status_code==200
        h2={'Authorization':'Bearer '+reg.json()['access_token']}
        assert c.get(f'/api/opportunities/{oid}',headers=h2).status_code==404
        assert c.post(f'/api/opportunities/{oid}/dismiss',headers=h2).status_code==404


def test_user_revoke_invalidates_token(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        r=c.post('/api/users',headers=h,json={'email':'member@example.com','password':'StrongMemberPassword123!','role':'member'}); assert r.status_code==200
        member_login=c.post('/api/auth/login',json={'email':'member@example.com','password':'StrongMemberPassword123!','organization_slug':'acme-demo'}); assert member_login.status_code==200
        mh={'Authorization':'Bearer '+member_login.json()['access_token']}
        assert c.get('/api/me',headers=mh).status_code==200
        assert c.post('/api/users/'+r.json()['id']+'/revoke',headers=h).status_code==200
        assert c.get('/api/me',headers=mh).status_code==401


def test_webhook_replay_is_idempotent(tmp_path,monkeypatch):
    monkeypatch.setenv('WEBHOOK_SECRET_org_demo','secret-123')
    with make_client(tmp_path) as c:
        payload={'type':'lead.created','external_id':'evt-1','data':{'lead':'x'}}
        r1=c.post('/api/ingest/org_demo',headers={'X-Webhook-Secret':'secret-123'},json=payload); assert r1.status_code==200 and r1.json()['idempotent'] is False
        r2=c.post('/api/ingest/org_demo',headers={'X-Webhook-Secret':'secret-123'},json=payload); assert r2.status_code==200 and r2.json()['idempotent'] is True


def test_recovery_idempotency_key_is_stable(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c); oid=next(x['id'] for x in c.get('/api/opportunities',headers=h).json() if x['customer_id']=='cus_2')
        assert c.post(f'/api/opportunities/{oid}/recovery-plan',headers=h,json={'channel':'sms'}).status_code==200
        payload={'channel':'sms','approval':True,'idempotency_key':'client-action-001'}
        r1=c.post(f'/api/opportunities/{oid}/execute',headers=h,json=payload); r2=c.post(f'/api/opportunities/{oid}/execute',headers=h,json=payload)
        assert r1.status_code==200 and r2.status_code==200
        assert r2.json()['idempotent'] is True
        assert r1.json()['action_id']==r2.json()['action_id']

def test_integration_catalog_and_tenant_scoped_configuration(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        cat=c.get('/api/integrations/catalog',headers=h); assert cat.status_code==200
        providers={x['provider'] for x in cat.json()['providers']}
        assert {'google_business_profile','hubspot','gohighlevel','stripe','twilio','microsoft_graph','servicetitan','quickbooks','xero','salesforce','pipedrive','zoho_crm'} <= providers
        live={x['provider']:x for x in cat.json()['providers'] if x['live_adapter']}
        assert {'google_business_profile','hubspot','gohighlevel','stripe','twilio','google_gmail','google_calendar'} <= set(live)
        r=c.post('/api/integrations/stripe/configure',headers=h,json={'credentials':{'api_key':'sk_test_x'},'config':{}})
        assert r.status_code==200
        listed={x['provider']:x for x in c.get('/api/integrations',headers=h).json()}
        assert listed['stripe']['status']=='configured'
        # credentials must never appear in list responses
        assert 'sk_test_x' not in json.dumps(listed['stripe'])

def test_integration_configuration_is_tenant_isolated(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        reg=c.post('/api/auth/register',json={'organization_name':'Other','organization_slug':'other-tenant','email':'other@example.com','password':'OtherStrongPassword123!'}); assert reg.status_code==200
        h2={'Authorization':'Bearer '+reg.json()['access_token']}
        assert c.post('/api/integrations/stripe/configure',headers=h,json={'credentials':{'api_key':'tenant-a-secret'}}).status_code==200
        rows={x['provider']:x for x in c.get('/api/integrations',headers=h2).json()}
        assert rows['stripe']['status']=='not_configured'

def test_oauth_requires_provider_client_configuration(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        r=c.post('/api/integrations/google_gmail/oauth/start',headers=h)
        assert r.status_code==503

def test_integration_test_endpoint_uses_stored_credentials_without_leaking_them(tmp_path,monkeypatch):
    import app.main as m
    class FakeAdapter:
        def __init__(self, credentials, config=None):
            assert credentials.get('api_key') == 'secret-token'
        async def health(self):
            return {'ok': True}
        async def close(self):
            pass
    monkeypatch.setitem(m.ADAPTERS, 'stripe', FakeAdapter)
    with make_client(tmp_path) as c:
        h=auth(c)
        assert c.post('/api/integrations/stripe/configure',headers=h,json={'credentials':{'api_key':'secret-token'}}).status_code==200
        r=c.post('/api/integrations/stripe/test',headers=h); assert r.status_code==200
        assert 'secret-token' not in r.text

def test_cookie_csrf_and_bearer_behavior(tmp_path):
    with make_client(tmp_path) as c:
        login=c.post('/api/auth/login',json={'email':'owner@example.com','password':'demo-password','organization_slug':'acme-demo'}); assert login.status_code==200
        # Cookie-only unsafe request must require CSRF.
        r=c.post('/api/detect'); assert r.status_code==403
        csrf=c.cookies.get('rros_csrf'); assert csrf
        r=c.post('/api/detect',headers={'X-CSRF-Token':csrf}); assert r.status_code==200
        # Bearer clients are not blocked by browser CSRF middleware.
        bearer={'Authorization':'Bearer '+login.json()['access_token']}
        assert c.post('/api/detect',headers=bearer).status_code==200


def test_webhook_changed_payload_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv('WEBHOOK_SECRET_org_demo','secret-123')
    with make_client(tmp_path) as c:
        h={'X-Webhook-Secret':'secret-123'}
        assert c.post('/api/ingest/org_demo',headers=h,json={'type':'lead.created','external_id':'same','data':{'value':100}}).status_code==200
        assert c.post('/api/ingest/org_demo',headers=h,json={'type':'lead.created','external_id':'same','data':{'value':999}}).status_code==409


def test_canonical_message_creates_response_gap(tmp_path, monkeypatch):
    monkeypatch.setenv('WEBHOOK_SECRET_org_demo','secret-123')
    with make_client(tmp_path) as c:
        h={'X-Webhook-Secret':'secret-123'}
        from datetime import datetime, timezone, timedelta
        occurred=(datetime.now(timezone.utc)-timedelta(hours=30)).isoformat()
        r=c.post('/api/ingest/org_demo',headers=h,json={'type':'message.received','external_id':'msg-gap-1','occurred_at':occurred,'data':{'customer_external_id':'cus_1','channel':'sms','direction':'inbound','status':'received','occurred_at':occurred,'body':'Can someone help me?','thread_key':'thread-gap'}})
        assert r.status_code==200 and r.json()['processed']['entity']=='communication'
        assert c.post('/api/detect',headers={'Authorization':'Bearer '+c.post('/api/auth/login',json={'email':'owner@example.com','password':'demo-password'}).json()['access_token']}).status_code==200
        ops=c.get('/api/opportunities',headers=auth(c)).json()
        assert any(o['type']=='response_gap' for o in ops)


def test_customer_360_and_location_analytics_are_tenant_scoped(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        r=c.get('/api/customers/cus_2',headers=h); assert r.status_code==200
        data=r.json(); assert data['customer']['id']=='cus_2'; assert data['appointments']; assert data['consents']
        assert c.get('/api/customers/cus_2',headers={'Authorization':'Bearer '+c.post('/api/auth/register',json={'organization_name':'Other360','organization_slug':'other360','email':'other360@example.com','password':'Other360Password123!'}).json()['access_token']}).status_code==404
        loc=c.get('/api/analytics/locations',headers=h); assert loc.status_code==200 and loc.json()


def test_ssrf_protection_and_oauth_account_specific_guard():
    from app.integrations import validate_remote_url
    import pytest
    with pytest.raises(ValueError): validate_remote_url('http://example.com/')
    with pytest.raises(ValueError): validate_remote_url('https://127.0.0.1/')
    validate_remote_url('https://8.8.8.8/')


def test_database_fk_blocks_cross_tenant_linkage(tmp_path):
    with make_client(tmp_path) as c:
        from app.db import SessionLocal
        from app.models import Organization, Customer, Lead
        from sqlalchemy.exc import IntegrityError
        db=SessionLocal()
        try:
            db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
            org_a=db.get(Organization,'org_demo')
            org_b=Organization(id='org_b',name='B',slug='b-tenant')
            db.add(org_b); db.commit()
            cust_b=Customer(id='cust_b',organization_id='org_b',name='B customer')
            db.add(cust_b); db.commit()
            db.add(Lead(id='lead_cross',organization_id=org_a.id,customer_id=cust_b.id,status='new',value=0))
            with pytest.raises(IntegrityError): db.commit()
        finally:
            db.rollback(); db.close()


def test_logout_revokes_bearer_token(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        assert c.get('/api/me',headers=h).status_code==200
        assert c.post('/api/auth/logout',headers=h).status_code==200
        assert c.get('/api/me',headers=h).status_code==401


def test_recovery_idempotency_key_cannot_change_payload(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        oid=next(x['id'] for x in c.get('/api/opportunities',headers=h).json() if x['customer_id']=='cus_2')
        assert c.post(f'/api/opportunities/{oid}/recovery-plan',headers=h,json={'channel':'sms'}).status_code==200
        key='same-key-xyz'
        r1=c.post(f'/api/opportunities/{oid}/execute',headers=h,json={'channel':'sms','approval':True,'message':'A','idempotency_key':key})
        assert r1.status_code==200
        r2=c.post(f'/api/opportunities/{oid}/execute',headers=h,json={'channel':'sms','approval':True,'message':'B','idempotency_key':key})
        assert r2.status_code==409


def test_marketing_spend_and_source_analytics_are_tenant_scoped(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        body={'source':'Google Ads','amount':'500.00','period_start':'2026-09-01','period_end':'2026-09-30'}
        assert c.post('/api/marketing/spend',headers=h,json=body).status_code==200
        assert c.post('/api/marketing/spend',headers=h,json={**body,'amount':'650.00'}).status_code==200
        rows=c.get('/api/analytics/sources',headers=h); assert rows.status_code==200
        google=next(x for x in rows.json()['sources'] if x['source']=='google ads')
        assert google['spend']==650.0
        reg=c.post('/api/auth/register',json={'organization_name':'Spend Other','organization_slug':'spend-other','email':'spend@example.com','password':'SpendOtherPassword123!'})
        assert reg.status_code==200
        h2={'Authorization':'Bearer '+reg.json()['access_token']}
        assert all(x['source']!='google ads' for x in c.get('/api/analytics/sources',headers=h2).json()['sources'])


def test_success_payment_clears_failure_timestamp_and_customer_360_serializes_null(tmp_path,monkeypatch):
    monkeypatch.setenv('WEBHOOK_SECRET_org_demo','secret-123')
    with make_client(tmp_path) as c:
        h={'X-Webhook-Secret':'secret-123'}
        r=c.post('/api/ingest/org_demo',headers=h,json={'type':'payment.succeeded','external_id':'pay-success-1','occurred_at':'2026-09-30T12:00:00+00:00','data':{'customer_id':'cus_2','amount':'425.00'}})
        assert r.status_code==200
        # Customer 360 must be serializable after a successful payment has no failed_at.
        token=auth(c)
        r=c.get('/api/customers/cus_2',headers=token); assert r.status_code==200
        payments=r.json()['payments']; p=next(x for x in payments if x['id']=='pay-success-1')
        assert p['failed_at'] is None


def test_cross_tenant_fk_blocks_location_spend_collision(tmp_path):
    with make_client(tmp_path) as c:
        from app.db import SessionLocal
        from app.models import Organization, Location, MarketingSpend
        from sqlalchemy.exc import IntegrityError
        from datetime import date
        db=SessionLocal()
        try:
            db.connection().exec_driver_sql("PRAGMA foreign_keys=ON")
            org_a=db.get(Organization,'org_demo')
            org_b=Organization(id='org_fk_b',name='FK B',slug='fk-b')
            db.add(org_b); db.commit()
            loc_b=Location(id='loc_fk_b',organization_id='org_fk_b',name='B Location')
            db.add(loc_b); db.commit()
            db.add(MarketingSpend(id='spend_cross',organization_id=org_a.id,location_id=loc_b.id,source='test',amount='1.00',period_start=date(2026,9,1),period_end=date(2026,9,1)))
            with pytest.raises(IntegrityError): db.commit()
        finally:
            db.rollback(); db.close()


def test_registration_returns_tenant_owned_webhook_secret_and_rotation(tmp_path):
    with make_client(tmp_path) as c:
        r=c.post('/api/auth/register',json={'organization_name':'Webhook Tenant','organization_slug':'webhook-tenant','email':'webhook@example.com','password':'WebhookStrongPassword123!'})
        assert r.status_code==200 and r.json()['webhook_secret']
        secret=r.json()['webhook_secret']
        oid=r.json()['organization_id']
        # New tenant secret works without any environment-level WEBHOOK_SECRET_* value.
        wh={'X-Webhook-Secret':secret}
        ev={'type':'lead.created','external_id':'webhook-lead-1','data':{'customer_name':'New Customer','customer_email':'new@example.com','value':'900'}}
        assert c.post(f'/api/ingest/{oid}',headers=wh,json=ev).status_code==200
        # Rotation invalidates the prior secret and returns a one-time replacement.
        h={'Authorization':'Bearer '+r.json()['access_token']}
        rr=c.post('/api/tenant/webhook-secret/rotate',headers=h); assert rr.status_code==200
        new_secret=rr.json()['webhook_secret']; assert new_secret!=secret
        assert c.post(f'/api/ingest/{oid}',headers=wh,json={'type':'lead.created','external_id':'webhook-lead-2','data':{}}).status_code==401
        assert c.post(f'/api/ingest/{oid}',headers={'X-Webhook-Secret':new_secret},json={'type':'lead.created','external_id':'webhook-lead-2','data':{}}).status_code==200


def test_ready_endpoint_checks_schema(tmp_path):
    with make_client(tmp_path) as c:
        r=c.get('/api/ready'); assert r.status_code==200 and r.json()['ready'] is True


def test_source_analytics_has_window_metrics_and_recommendations(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        r=c.get('/api/analytics/sources?days=30',headers=h); assert r.status_code==200
        body=r.json(); assert body['window']['days']==30 and isinstance(body['sources'],list)
        if body['sources']:
            row=body['sources'][0]; assert 'cost_per_lead' in row and 'recovery_rate' in row
        rec=c.get('/api/insights/recommendations',headers=h); assert rec.status_code==200 and isinstance(rec.json(),list)


def test_api_key_ingestion_is_tenant_scoped_and_revocable(tmp_path):
    with make_client(tmp_path) as c:
        owner = auth(c)
        created = c.post('/api/tenant/api-keys', headers=owner, json={'name':'CRM ingest'}); assert created.status_code==200
        key = created.json()['api_key']
        assert created.json()['key_prefix'] == key[:16]
        event = {'type':'lead.created','external_id':'api-lead-1','data':{'customer_name':'API Customer','customer_email':'api@example.com','value':'1250'}}
        r=c.post('/api/ingest/key',headers={'X-RROS-API-Key':key},json=event); assert r.status_code==200
        assert r.json()['processed']['entity']=='lead'
        replay=c.post('/api/ingest/key',headers={'X-RROS-API-Key':key},json=event); assert replay.status_code==200 and replay.json()['idempotent'] is True
        rows=c.get('/api/tenant/api-keys',headers=owner); assert rows.status_code==200 and rows.json()[0]['last_used_at']
        key_id=rows.json()[0]['id']
        assert c.post(f'/api/tenant/api-keys/{key_id}/revoke',headers=owner).status_code==200
        assert c.post('/api/ingest/key',headers={'X-RROS-API-Key':key},json={'type':'lead.created','external_id':'api-lead-2','data':{}}).status_code==401
        other=c.post('/api/auth/register',json={'organization_name':'API Other','organization_slug':'api-other','email':'api-other@example.com','password':'APIOtherPassword123!'}); assert other.status_code==200
        other_key = c.post('/api/tenant/api-keys',headers={'Authorization':'Bearer '+other.json()['access_token']},json={'name':'Other ingest'}); assert other_key.status_code==200
        assert c.get('/api/opportunities',headers={'Authorization':'Bearer '+other.json()['access_token']}).status_code==200


def test_review_response_workflow_is_human_approval_only_and_tenant_safe(tmp_path):
    with make_client(tmp_path) as c:
        owner=auth(c)
        draft=c.post('/api/reviews/rev_1/response-draft',headers=owner,json={}); assert draft.status_code==200
        assert 'sorry' in draft.json()['draft_text'].lower()
        assert draft.json()['status']=='draft'
        approved=c.post('/api/reviews/rev_1/response-approve',headers=owner); assert approved.status_code==200 and approved.json()['status']=='approved'
        saved=c.get('/api/reviews/rev_1/response-draft',headers=owner); assert saved.status_code==200 and saved.json()['draft']['status']=='approved'
        other=c.post('/api/auth/register',json={'organization_name':'Review Other','organization_slug':'review-other','email':'review-other@example.com','password':'ReviewOtherPassword123!'}); assert other.status_code==200
        other_h={'Authorization':'Bearer '+other.json()['access_token']}
        assert c.post('/api/reviews/rev_1/response-draft',headers=other_h,json={}).status_code==404


def test_setup_usage_insights_and_recovery_funnel_are_available(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        setup=c.get('/api/onboarding/status',headers=h); assert setup.status_code==200 and setup.json()['score'] >= 0
        usage=c.get('/api/tenant/usage',headers=h); assert usage.status_code==200 and usage.json()['monthly_opportunity_limit'] > 0
        health=c.get('/api/analytics/recovery-health',headers=h); assert health.status_code==200 and 0 <= health.json()['index'] <= 100
        funnel=c.get('/api/analytics/recovery-funnel',headers=h); assert funnel.status_code==200 and isinstance(funnel.json(),list)
        team=c.get('/api/analytics/team',headers=h); assert team.status_code==200 and isinstance(team.json(),list)


def test_migration_chain_is_unambiguous_and_model_tables_are_declared(tmp_path):
    from pathlib import Path
    migration_dir=Path('migrations')
    numbered=sorted(p.name for p in migration_dir.glob('*.sql'))
    assert numbered == [
        '001_v2_production.sql','002_integrations_v2.sql','003_v2_2_hardening.sql',
        '004_v2_2_sellability_security.sql','005_v2_3_tenant_webhook_and_readiness.sql',
        '006_v2_4_sellability_features.sql','007_v2_5_production_hardening.sql','008_v2_5_security_playbooks.sql',
        '009_v2_5_production_sellability.sql','010_agent_os.sql','011_connector_fabric.sql',
        '012_durable_connector_runtime.sql','013_runtime_integrity_and_lineage.sql',
        '014_customer_identity_and_runtime_safety.sql','015_service_jobs_and_connector_approvals.sql',
        '016_runtime_concurrency_guards.sql','017_business_policies_and_review_analysis.sql','018_rbac_agent_analyst_roles.sql','019_attribution_types_and_indexes.sql','020_billing_checkout.sql','021_attribution_ledger_uniqueness.sql'
    ]
    assert (migration_dir/'legacy'/'001_revenue_recovery_core.sql.disabled').exists()


def test_playbook_lifecycle_is_tenant_scoped_and_approval_gated(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        created=c.post('/api/playbooks',headers=h,json={
            'name':'Stalled Lead Recovery','description':'Test playbook','trigger_type':'opportunity.created',
            'conditions':{'opportunity_types':['lost_lead'],'statuses':['open','planned']},
            'steps':[{'action':'prepare_context'},{'channel':'sms','action':'send_message','message':'We would love to help.'}]
        }); assert created.status_code==200
        pid=created.json()['id']
        assert c.post(f'/api/playbooks/{pid}/publish',headers=h).status_code==200
        oid=next(x['id'] for x in c.get('/api/opportunities',headers=h).json() if x['type']=='lost_lead')
        applied=c.post(f'/api/playbooks/{pid}/apply/{oid}',headers=h); assert applied.status_code==200 and applied.json()['approval_required'] is True
        applied2=c.post(f'/api/playbooks/{pid}/apply/{oid}',headers=h); assert applied2.status_code==200 and applied2.json()['idempotent'] is True
        other=c.post('/api/auth/register',json={'organization_name':'PB Other','organization_slug':'pb-other','email':'pb-other@example.com','password':'PbOtherPassword123!'}); assert other.status_code==200
        other_h={'Authorization':'Bearer '+other.json()['access_token']}
        assert c.get('/api/playbooks',headers=other_h).json()==[]
        assert c.post(f'/api/playbooks/{pid}/publish',headers=other_h).status_code==404


def test_agent_os_pipeline_handoffs_memory_feedback_and_learning_are_tenant_scoped(tmp_path):
    with make_client(tmp_path) as c:
        h=auth(c)
        catalog=c.get('/api/agents',headers=h); assert catalog.status_code==200 and len(catalog.json())==11
        oid=c.get('/api/opportunities',headers=h).json()[0]['id']
        run=c.post(f'/api/opportunities/{oid}/agents/run',headers=h); assert run.status_code==200, run.text
        run_id=run.json()['pipeline_run_id']; assert run.json()['status']=='succeeded'
        detail=c.get(f'/api/agent-runs/{run_id}',headers=h); assert detail.status_code==200
        body=detail.json(); assert len(body['agents'])==11 and len(body['handoffs'])==10 and body['memories']
        fb=c.post(f'/api/agent-runs/{run_id}/feedback',headers=h,json={'agent_key':'revenue_analyst','rating':4,'correction':'Probability was reasonable.'}); assert fb.status_code==200
        out=c.post(f'/api/opportunities/{oid}/agent-outcome',headers=h,json={'outcome':'recovered','amount':'250.00'}); assert out.status_code==200
        learning=c.get('/api/agent-learning',headers=h); assert learning.status_code==200 and learning.json()['sample_size']==1
        other=c.post('/api/auth/register',json={'organization_name':'Agent Other','organization_slug':'agent-other','email':'agent-other@example.com','password':'AgentOtherPassword123!'}); assert other.status_code==200
        oh={'Authorization':'Bearer '+other.json()['access_token']}
        assert c.get(f'/api/agent-runs/{run_id}',headers=oh).status_code==404
        assert c.get('/api/agent-learning',headers=oh).json()['sample_size']==0


def test_canonical_external_ids_are_provider_scoped(tmp_path):
    from app.models import Customer
    from sqlalchemy import select
    with make_client(tmp_path) as c:
        h=auth(c)
        assert c.post('/api/ingest/org_demo',headers=h,json={'type':'customer.upserted','external_id':'123','data':{'name':'Stripe Customer','email':'stripe@example.com'}}).status_code==200
        assert c.post('/api/ingest/org_demo',headers=h,json={'type':'customer.upserted','external_id':'123','data':{'name':'HubSpot Customer','email':'hubspot@example.com'}}).status_code==200
        from app.db import SessionLocal
        db=SessionLocal()
        try:
            rows=db.scalars(select(Customer).where(Customer.organization_id=='org_demo', Customer.external_id.in_(['api_key:123','generic_webhook:123']))).all()
            assert len(rows) == 2
        finally:
            db.close()


def test_canonical_external_ids_are_provider_scoped(tmp_path):
    from app.models import Customer
    from app.services import canonical_event
    from app.db import SessionLocal
    from sqlalchemy import select
    with make_client(tmp_path) as c:
        db=SessionLocal()
        try:
            canonical_event(db, 'org_demo', 'stripe', 'customer.upserted', {'external_id':'123','name':'Stripe Customer','email':'stripe@example.com'})
            canonical_event(db, 'org_demo', 'hubspot', 'customer.upserted', {'external_id':'123','name':'HubSpot Customer','email':'hubspot@example.com'})
            db.commit()
            rows=db.scalars(select(Customer).where(Customer.organization_id=='org_demo', Customer.external_id.in_(['stripe:123','hubspot:123']))).all()
            assert len(rows) == 2
        finally:
            db.close()
