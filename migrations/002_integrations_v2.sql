-- Revenue Recovery OS v2 integration layer.
-- Run against an existing PostgreSQL v2 database after 001_v2_production.sql.
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS credentials_ciphertext TEXT;
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS account_metadata_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS config_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE integrations ADD COLUMN IF NOT EXISTS last_error TEXT;
CREATE TABLE IF NOT EXISTS integration_oauth_states (
  id VARCHAR(64) PRIMARY KEY,
  organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id),
  user_id VARCHAR(64) NOT NULL REFERENCES users(id),
  provider VARCHAR(60) NOT NULL,
  state_hash VARCHAR(64) NOT NULL UNIQUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  expires_at TIMESTAMPTZ NOT NULL,
  used BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_oauth_state_org ON integration_oauth_states(organization_id);
CREATE TABLE IF NOT EXISTS integration_events (
  id BIGSERIAL PRIMARY KEY,
  organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id),
  integration_id VARCHAR(64) NOT NULL REFERENCES integrations(id),
  provider VARCHAR(60) NOT NULL,
  external_id VARCHAR(255),
  event_type VARCHAR(120) NOT NULL,
  payload_json TEXT NOT NULL,
  processed BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(organization_id, provider, external_id)
);
CREATE INDEX IF NOT EXISTS ix_integration_event_org_provider ON integration_events(organization_id, provider);
