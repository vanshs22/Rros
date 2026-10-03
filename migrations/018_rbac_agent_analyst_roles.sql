-- 018 — add checklist roles without weakening tenant isolation.
ALTER TABLE users DROP CONSTRAINT IF EXISTS ck_user_role;
ALTER TABLE users ADD CONSTRAINT ck_user_role
  CHECK (role IN ('owner','admin','manager','agent','analyst','member','viewer'));
