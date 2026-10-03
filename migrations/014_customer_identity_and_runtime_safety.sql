-- 014 — cross-provider customer identity resolution and execution safety.
CREATE TABLE IF NOT EXISTS customer_identities (
  id varchar(64) PRIMARY KEY,
  organization_id varchar(64) NOT NULL REFERENCES organizations(id),
  customer_id varchar(64) NOT NULL,
  provider varchar(60) NOT NULL,
  external_id varchar(255) NOT NULL,
  source varchar(40) NOT NULL DEFAULT 'connector',
  first_seen_at timestamptz NOT NULL DEFAULT now(),
  last_seen_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_customer_identity_org_provider_external UNIQUE (organization_id, provider, external_id),
  CONSTRAINT fk_customer_identity_org_customer FOREIGN KEY (organization_id, customer_id)
    REFERENCES customers(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_customer_identity_org_customer ON customer_identities(organization_id, customer_id);
ALTER TABLE customer_identities ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS customer_identities_org_isolation ON customer_identities;
CREATE POLICY customer_identities_org_isolation ON customer_identities
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());
