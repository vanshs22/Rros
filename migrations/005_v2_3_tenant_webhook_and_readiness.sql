-- Revenue Recovery OS v2.3 hardening.
-- Tenant-owned webhook secrets, schema readiness support, and safer inbound authentication.
-- Prerequisite: 001_v2_production.sql + 002_integrations_v2.sql + 003_v2_2_hardening.sql + 004_v2_2_sellability_security.sql.

ALTER TABLE organizations ADD COLUMN IF NOT EXISTS webhook_secret_hash TEXT;
ALTER TABLE organizations ADD COLUMN IF NOT EXISTS webhook_secret_version INTEGER NOT NULL DEFAULT 1;

CREATE OR REPLACE FUNCTION rros_webhook_tenant_lookup(p_organization_id TEXT)
RETURNS TABLE(organization_id TEXT, active BOOLEAN, webhook_secret_hash TEXT, webhook_secret_version INTEGER)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
  SELECT o.id, o.active, o.webhook_secret_hash, o.webhook_secret_version
  FROM organizations o
  WHERE o.id = p_organization_id
  LIMIT 1
$$;
GRANT EXECUTE ON FUNCTION rros_webhook_tenant_lookup(TEXT) TO PUBLIC;

CREATE INDEX IF NOT EXISTS ix_webhook_events_org_external ON webhook_events(organization_id, external_id);
CREATE INDEX IF NOT EXISTS ix_integrations_org_status ON integrations(organization_id, status);
CREATE INDEX IF NOT EXISTS ix_customers_org_email ON customers(organization_id, lower(email));
CREATE INDEX IF NOT EXISTS ix_customers_org_phone ON customers(organization_id, phone);
CREATE INDEX IF NOT EXISTS ix_transactions_org_paid ON transactions(organization_id, status, paid_at DESC);
CREATE INDEX IF NOT EXISTS ix_leads_org_created ON leads(organization_id, created_at DESC);

-- Prevent accidental zero/negative balances and invalid invoice totals.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_invoice_amounts') THEN
    ALTER TABLE invoices ADD CONSTRAINT ck_invoice_amounts CHECK (total >= 0 AND amount_due >= 0 AND amount_due <= total);
  END IF;
END $$;
