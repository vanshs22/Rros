-- 016 — concurrency guards for tenant/resource job serialization.
-- These partial unique indexes prevent concurrent scheduler/manual syncs for the
-- same tenant+integration+resource and concurrent agent runs for the same opportunity.
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_connector_sync_slot
  ON integration_jobs (organization_id, integration_id, resource)
  WHERE job_type = 'connector_sync' AND status IN ('queued','running','retrying');

CREATE UNIQUE INDEX IF NOT EXISTS uq_active_agent_opportunity_slot
  ON integration_jobs (organization_id, resource)
  WHERE job_type = 'agent_pipeline' AND status IN ('queued','running','retrying');
