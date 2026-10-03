-- 021 — one canonical attribution classification per transaction/opportunity pair.
CREATE UNIQUE INDEX IF NOT EXISTS uq_attr_transaction_opportunity
  ON attribution_links(organization_id, transaction_id, opportunity_id);
