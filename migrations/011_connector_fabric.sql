-- 011 Connector Fabric: durable action ledger and stronger integration idempotency.
CREATE TABLE IF NOT EXISTS connector_actions (
  id varchar(64) PRIMARY KEY,
  organization_id varchar(64) NOT NULL REFERENCES organizations(id),
  integration_id varchar(64) NOT NULL,
  provider varchar(60) NOT NULL,
  operation varchar(120) NOT NULL,
  idempotency_key varchar(200) NOT NULL,
  request_hash varchar(64) NOT NULL,
  status varchar(30) NOT NULL DEFAULT 'pending',
  attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  external_id varchar(255),
  request_json text NOT NULL DEFAULT '{}',
  response_json text NOT NULL DEFAULT '{}',
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  completed_at timestamptz,
  CONSTRAINT uq_connector_action_org_key UNIQUE (organization_id, idempotency_key),
  CONSTRAINT uq_connector_action_org_id_ref UNIQUE (organization_id, id),
  CONSTRAINT fk_connector_action_org_integration FOREIGN KEY (organization_id, integration_id)
    REFERENCES integrations(organization_id, id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_connector_action_org_status ON connector_actions(organization_id, status, created_at);
CREATE INDEX IF NOT EXISTS ix_connector_action_provider_status ON connector_actions(provider, status, created_at);

ALTER TABLE integration_sync_states ADD COLUMN IF NOT EXISTS records_failed integer NOT NULL DEFAULT 0;
ALTER TABLE integration_sync_states ADD COLUMN IF NOT EXISTS last_error_count integer NOT NULL DEFAULT 0;

ALTER TABLE integration_events ADD COLUMN IF NOT EXISTS payload_hash varchar(64);
CREATE INDEX IF NOT EXISTS ix_integration_event_org_provider_created ON integration_events(organization_id, provider, created_at);
