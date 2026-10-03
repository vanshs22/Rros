-- Revenue Recovery OS v2.2 follow-up hardening + sellability migration.
-- Prerequisite: 001_v2_production.sql + 002_integrations_v2.sql + 003_v2_2_hardening.sql.
-- Review/back up production data before applying.

-- 1) Payment lifecycle correctness: successful/pending payments do not require a failure timestamp.
ALTER TABLE payments ALTER COLUMN failed_at DROP NOT NULL;

-- 2) Ensure consent ownership is tenant-safe at the database layer.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_consent_org_customer') THEN
    ALTER TABLE channel_consents ADD CONSTRAINT fk_consent_org_customer
      FOREIGN KEY (organization_id, customer_id) REFERENCES customers(organization_id, id);
  END IF;
END $$;

-- 3) Marketing-source ROI: spend is explicitly entered by source and period, while revenue
-- remains tied to the canonical lead -> opportunity -> transaction lineage.
CREATE TABLE IF NOT EXISTS marketing_spend (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  location_id TEXT,
  source TEXT NOT NULL,
  amount NUMERIC(18,2) NOT NULL,
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_spend_org_period UNIQUE (organization_id, location_id, source, period_start, period_end),
  CONSTRAINT fk_spend_org_location FOREIGN KEY (organization_id, location_id) REFERENCES locations(organization_id, id),
  CONSTRAINT ck_spend_amount_nonnegative CHECK (amount >= 0),
  CONSTRAINT ck_spend_period CHECK (period_end >= period_start)
);
CREATE INDEX IF NOT EXISTS ix_spend_org_source_period ON marketing_spend(organization_id, source, period_start, period_end);
CREATE UNIQUE INDEX IF NOT EXISTS ux_spend_org_period_effective ON marketing_spend(organization_id, COALESCE(location_id, ''), source, period_start, period_end);
ALTER TABLE marketing_spend ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS rros_tenant_policy ON marketing_spend;
CREATE POLICY rros_tenant_policy ON marketing_spend
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());

-- 4) Domain checks for tables whose ORM already validates the same rules.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_consent_channel') THEN
    ALTER TABLE channel_consents ADD CONSTRAINT ck_consent_channel CHECK (channel IN ('sms','email','whatsapp','voice'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_plan_channel') THEN
    ALTER TABLE recovery_plans ADD CONSTRAINT ck_plan_channel CHECK (channel IN ('sms','email','whatsapp','voice'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_action_channel') THEN
    ALTER TABLE recovery_actions ADD CONSTRAINT ck_action_channel CHECK (channel IN ('sms','email','whatsapp','voice'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_action_status') THEN
    ALTER TABLE recovery_actions ADD CONSTRAINT ck_action_status CHECK (status IN ('pending','approved','queued','sending','sent','delivered','simulated','failed','cancelled'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_approval_decision') THEN
    ALTER TABLE recovery_approvals ADD CONSTRAINT ck_approval_decision CHECK (decision IN ('approved','rejected'));
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_payment_amount_nonnegative') THEN
    ALTER TABLE payments ADD CONSTRAINT ck_payment_amount_nonnegative CHECK (amount >= 0);
  END IF;
END $$;

-- 5) Production database role note: the auth lookup function is intentionally narrow but
-- requires execute permission for the application DB role. If migrations and runtime use
-- different roles, grant execute on this function to the runtime role explicitly.
-- GRANT EXECUTE ON FUNCTION rros_auth_lookup(text,text) TO <RROS_APP_ROLE>;
