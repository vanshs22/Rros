-- 019 — complete attribution taxonomy and high-value query indexes.
ALTER TABLE attribution_links DROP CONSTRAINT IF EXISTS ck_attr_classification;
ALTER TABLE attribution_links ADD CONSTRAINT ck_attr_classification
  CHECK (classification IN ('direct','assisted','influenced','estimated','unattributed'));

CREATE INDEX IF NOT EXISTS ix_customers_org_lifecycle ON customers(organization_id, lifecycle_status);
CREATE INDEX IF NOT EXISTS ix_leads_org_status_created ON leads(organization_id, status, created_at);
CREATE INDEX IF NOT EXISTS ix_reviews_org_rating_created ON reviews(organization_id, rating, created_at);
CREATE INDEX IF NOT EXISTS ix_transactions_org_status_paid ON transactions(organization_id, status, paid_at);
CREATE INDEX IF NOT EXISTS ix_comm_events_org_customer_time ON communication_events(organization_id, customer_id, occurred_at);
CREATE INDEX IF NOT EXISTS ix_webhook_events_org_status_received ON webhook_events(organization_id, status, received_at);
CREATE INDEX IF NOT EXISTS ix_audit_logs_org_time ON audit_logs(organization_id, created_at);
