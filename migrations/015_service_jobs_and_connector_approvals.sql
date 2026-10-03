-- 015 — canonical field-service jobs + durable connector approval metadata.
CREATE TABLE IF NOT EXISTS service_jobs (
  id varchar(64) PRIMARY KEY,
  organization_id varchar(64) NOT NULL REFERENCES organizations(id),
  customer_id varchar(64),
  external_id varchar(160),
  provider varchar(60) NOT NULL,
  title varchar(500),
  status varchar(60) NOT NULL DEFAULT 'open',
  value numeric(18,2) NOT NULL DEFAULT 0,
  scheduled_at timestamptz,
  completed_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_service_job_org_external UNIQUE (organization_id, external_id),
  CONSTRAINT uq_service_job_org_id_ref UNIQUE (organization_id, id),
  CONSTRAINT fk_service_job_org_customer FOREIGN KEY (organization_id, customer_id)
    REFERENCES customers(organization_id, id) ON DELETE SET NULL,
  CONSTRAINT ck_service_job_value_nonnegative CHECK (value >= 0)
);
CREATE INDEX IF NOT EXISTS ix_service_job_org_customer ON service_jobs(organization_id, customer_id);
CREATE INDEX IF NOT EXISTS ix_service_job_org_status ON service_jobs(organization_id, status);
CREATE INDEX IF NOT EXISTS ix_service_job_org_scheduled ON service_jobs(organization_id, scheduled_at);
ALTER TABLE service_jobs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS service_jobs_org_isolation ON service_jobs;
CREATE POLICY service_jobs_org_isolation ON service_jobs
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());

ALTER TABLE connector_actions ADD COLUMN IF NOT EXISTS approved_by_user_id varchar(64);
ALTER TABLE connector_actions ADD COLUMN IF NOT EXISTS approved_at timestamptz;
ALTER TABLE connector_actions ADD COLUMN IF NOT EXISTS approval_source varchar(60);
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_connector_action_org_approver') THEN
    ALTER TABLE connector_actions
      ADD CONSTRAINT fk_connector_action_org_approver
      FOREIGN KEY (organization_id, approved_by_user_id)
      REFERENCES users(organization_id, id);
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS ix_connector_action_org_approver ON connector_actions(organization_id, approved_by_user_id);
