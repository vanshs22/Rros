-- Revenue Recovery OS Agent OS
CREATE TABLE IF NOT EXISTS agent_definitions (
 id VARCHAR(64) PRIMARY KEY, organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id),
 key VARCHAR(80) NOT NULL, name VARCHAR(160) NOT NULL, description TEXT NOT NULL,
 autonomy_mode VARCHAR(30) NOT NULL DEFAULT 'recommend', version INTEGER NOT NULL DEFAULT 1,
 status VARCHAR(30) NOT NULL DEFAULT 'active', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT uq_agent_def_org_key UNIQUE(organization_id,key), CONSTRAINT uq_agent_def_org_id_ref UNIQUE(organization_id,id), CONSTRAINT ck_agent_def_status CHECK(status IN ('active','paused','disabled'))
);
CREATE INDEX IF NOT EXISTS ix_agent_def_org ON agent_definitions(organization_id);
CREATE TABLE IF NOT EXISTS agent_runs (
 id VARCHAR(64) PRIMARY KEY, organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id),
 opportunity_id VARCHAR(64), parent_run_id VARCHAR(64), agent_definition_id VARCHAR(64), status VARCHAR(30) NOT NULL DEFAULT 'queued',
 pipeline_version VARCHAR(40) NOT NULL DEFAULT '1.0', requested_by VARCHAR(64), input_json TEXT NOT NULL DEFAULT '{}', output_json TEXT NOT NULL DEFAULT '{}',
 started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT uq_agent_run_org_id_ref UNIQUE(organization_id,id),
 CONSTRAINT fk_agent_run_org_opp FOREIGN KEY(organization_id,opportunity_id) REFERENCES revenue_opportunities(organization_id,id),
 CONSTRAINT fk_agent_run_org_def FOREIGN KEY(organization_id,agent_definition_id) REFERENCES agent_definitions(organization_id,id),
 CONSTRAINT ck_agent_run_status CHECK(status IN ('queued','running','succeeded','failed','cancelled'))
);
CREATE INDEX IF NOT EXISTS ix_agent_runs_org_created ON agent_runs(organization_id,created_at);
CREATE TABLE IF NOT EXISTS agent_handoffs (
 id VARCHAR(64) PRIMARY KEY, organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id), pipeline_run_id VARCHAR(64) NOT NULL,
 from_agent_key VARCHAR(80) NOT NULL, to_agent_key VARCHAR(80) NOT NULL, payload_json TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT fk_handoff_org_run FOREIGN KEY(organization_id,pipeline_run_id) REFERENCES agent_runs(organization_id,id)
);
CREATE INDEX IF NOT EXISTS ix_handoff_org_run ON agent_handoffs(organization_id,pipeline_run_id);
CREATE TABLE IF NOT EXISTS agent_memory (
 id VARCHAR(64) PRIMARY KEY, organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id), scope_type VARCHAR(30) NOT NULL, scope_id VARCHAR(160) NOT NULL,
 memory_type VARCHAR(80) NOT NULL, content_json TEXT NOT NULL, confidence NUMERIC(7,5) NOT NULL DEFAULT 0, source_run_id VARCHAR(64), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT ck_agent_memory_confidence CHECK(confidence>=0 AND confidence<=1)
);
CREATE INDEX IF NOT EXISTS ix_agent_memory_scope ON agent_memory(organization_id,scope_type,scope_id);
CREATE TABLE IF NOT EXISTS agent_outcomes (
 id VARCHAR(64) PRIMARY KEY, organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id), opportunity_id VARCHAR(64) NOT NULL,
 outcome VARCHAR(50) NOT NULL, amount NUMERIC(18,2) NOT NULL DEFAULT 0, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT fk_agent_outcome_org_opp FOREIGN KEY(organization_id,opportunity_id) REFERENCES revenue_opportunities(organization_id,id)
);
CREATE INDEX IF NOT EXISTS ix_agent_outcome_org_created ON agent_outcomes(organization_id,created_at);
CREATE TABLE IF NOT EXISTS agent_feedback (
 id VARCHAR(64) PRIMARY KEY, organization_id VARCHAR(64) NOT NULL REFERENCES organizations(id), agent_run_id VARCHAR(64) NOT NULL,
 agent_key VARCHAR(80) NOT NULL, rating INTEGER NOT NULL, correction TEXT, created_by_user_id VARCHAR(64), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 CONSTRAINT fk_feedback_org_run FOREIGN KEY(organization_id,agent_run_id) REFERENCES agent_runs(organization_id,id),
 CONSTRAINT fk_feedback_org_user FOREIGN KEY(organization_id,created_by_user_id) REFERENCES users(organization_id,id),
 CONSTRAINT ck_agent_feedback_rating CHECK(rating BETWEEN 1 AND 5)
);
