-- 012 — Durable connector/runtime queue.
-- PostgreSQL is the production source of truth; workers claim jobs using SKIP LOCKED.
CREATE TABLE IF NOT EXISTS integration_jobs (
  id varchar(64) PRIMARY KEY,
  organization_id varchar(64) NOT NULL REFERENCES organizations(id),
  integration_id varchar(64),
  connector_action_id varchar(64),
  provider varchar(60) NOT NULL,
  job_type varchar(40) NOT NULL,
  resource varchar(100),
  payload_json text NOT NULL DEFAULT '{}',
  dedupe_key varchar(300) NOT NULL,
  request_hash varchar(64) NOT NULL,
  status varchar(30) NOT NULL DEFAULT 'queued',
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  max_attempts integer NOT NULL DEFAULT 8 CHECK (max_attempts BETWEEN 1 AND 100),
  available_at timestamptz NOT NULL DEFAULT now(),
  locked_by varchar(120),
  locked_at timestamptz,
  lease_until timestamptz,
  last_heartbeat_at timestamptz,
  last_error text,
  result_json text NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz,
  CONSTRAINT uq_integration_job_org_dedupe UNIQUE (organization_id, dedupe_key),
  CONSTRAINT fk_job_org_integration FOREIGN KEY (organization_id, integration_id) REFERENCES integrations(organization_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_job_org_action FOREIGN KEY (organization_id, connector_action_id) REFERENCES connector_actions(organization_id, id) ON DELETE CASCADE,
  CONSTRAINT ck_job_type CHECK (job_type IN ('connector_sync','connector_action','detect_org','agent_pipeline')),
  CONSTRAINT ck_job_status CHECK (status IN ('queued','running','retrying','succeeded','failed','dead_letter','cancelled'))
);
CREATE INDEX IF NOT EXISTS ix_integration_job_claim ON integration_jobs(status, available_at, lease_until, created_at);
CREATE INDEX IF NOT EXISTS ix_integration_job_org_status ON integration_jobs(organization_id, status, created_at);

CREATE TABLE IF NOT EXISTS connector_rate_states (
  id varchar(64) PRIMARY KEY,
  organization_id varchar(64) NOT NULL REFERENCES organizations(id),
  provider varchar(60) NOT NULL,
  rate_limit_per_minute integer NOT NULL DEFAULT 120 CHECK (rate_limit_per_minute > 0),
  tokens_available numeric(18,4) NOT NULL DEFAULT 120 CHECK (tokens_available >= 0),
  last_refill_at timestamptz NOT NULL DEFAULT now(),
  consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
  circuit_state varchar(20) NOT NULL DEFAULT 'closed',
  circuit_open_until timestamptz,
  last_status_code integer,
  last_error text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_rate_state_org_provider UNIQUE (organization_id, provider),
  CONSTRAINT ck_circuit_state CHECK (circuit_state IN ('closed','open','half_open'))
);

CREATE TABLE IF NOT EXISTS connector_receipts (
  id varchar(64) PRIMARY KEY,
  organization_id varchar(64) NOT NULL REFERENCES organizations(id),
  integration_id varchar(64) NOT NULL,
  action_id varchar(64) NOT NULL,
  provider varchar(60) NOT NULL,
  operation varchar(120) NOT NULL,
  external_id varchar(255),
  status varchar(30) NOT NULL,
  provider_status varchar(60),
  response_hash varchar(64),
  response_json text NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_receipt_org_action UNIQUE (organization_id, action_id),
  CONSTRAINT fk_receipt_org_integration FOREIGN KEY (organization_id, integration_id) REFERENCES integrations(organization_id, id) ON DELETE CASCADE,
  CONSTRAINT fk_receipt_org_action_ref FOREIGN KEY (organization_id, action_id) REFERENCES connector_actions(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_connector_receipt_org_created ON connector_receipts(organization_id, created_at);

-- RLS defense-in-depth. Runtime tenant identity is supplied through app.current_org.
ALTER TABLE integration_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_rate_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_receipts ENABLE ROW LEVEL SECURITY;
CREATE POLICY integration_jobs_tenant_isolation ON integration_jobs USING (organization_id = current_setting('app.current_org', true));
CREATE POLICY connector_rate_states_tenant_isolation ON connector_rate_states USING (organization_id = current_setting('app.current_org', true));
CREATE POLICY connector_receipts_tenant_isolation ON connector_receipts USING (organization_id = current_setting('app.current_org', true));
