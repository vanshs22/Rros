# Revenue Recovery OS migrations

Apply numbered migrations in lexical order with the production migration runner. The canonical fresh-install chain is:

`001_v2_production.sql` → `002_integrations_v2.sql` → `003_v2_2_hardening.sql` → `004_v2_2_sellability_security.sql` → `005_v2_3_tenant_webhook_and_readiness.sql` → `006_v2_4_sellability_features.sql` → `007_v2_5_production_hardening.sql`

The former v1 migration is quarantined under `migrations/legacy/` and must not be used for new deployments.


## Production database roles

Run migrations with a dedicated schema-owner role. The API runtime role must be a separate login role with no BYPASSRLS and no ownership of application tables. Grant the runtime role EXECUTE on `rros_auth_lookup(text,text)` and `rros_api_key_lookup(text)` explicitly. The application startup check rejects unsafe table ownership or missing RLS.
