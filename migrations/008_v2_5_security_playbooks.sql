-- Revenue Recovery OS v2.5 security deployment checks + recovery playbooks.
-- Prerequisite: 001 through 007.

-- The runtime DB role must not own tenant tables and must not have BYPASSRLS.
-- This prevents a table-owner deployment from silently bypassing row-level security.
-- Production uses a dedicated migration/schema-owner role and a separate runtime role.
CREATE TABLE IF NOT EXISTS recovery_playbooks (
  id TEXT PRIMARY KEY,
  organization_id TEXT NOT NULL REFERENCES organizations(id),
  name TEXT NOT NULL,
  description TEXT,
  trigger_type TEXT NOT NULL,
  conditions_json TEXT NOT NULL DEFAULT '{}',
  steps_json TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  version INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT uq_playbook_org_name UNIQUE (organization_id, name),
  CONSTRAINT uq_playbook_org_id_ref UNIQUE (organization_id, id),
  CONSTRAINT ck_playbook_status CHECK (status IN ('draft','published','paused'))
);
CREATE INDEX IF NOT EXISTS ix_playbook_org_status_updated ON recovery_playbooks(organization_id,status,updated_at DESC);
ALTER TABLE recovery_playbooks ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS rros_tenant_policy ON recovery_playbooks;
CREATE POLICY rros_tenant_policy ON recovery_playbooks
  USING (organization_id = rros_current_org())
  WITH CHECK (organization_id = rros_current_org());

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_playbook_org') THEN
    ALTER TABLE recovery_playbooks ADD CONSTRAINT fk_playbook_org FOREIGN KEY (organization_id) REFERENCES organizations(id);
  END IF;
END $$;
