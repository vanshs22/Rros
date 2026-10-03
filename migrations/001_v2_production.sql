-- Revenue Recovery OS 2.0 canonical PostgreSQL schema.
-- This is a fresh-install migration. Existing 1.x databases should be migrated
-- with a reviewed data migration; do not run destructive DDL against production data.
create extension if not exists pgcrypto;

create table if not exists organizations (
  id text primary key, name text not null, slug text not null unique,
  active boolean not null default true, created_at timestamptz not null default now()
);
create table if not exists users (
  id text primary key, organization_id text not null references organizations(id),
  email text not null, password_hash text not null, role text not null default 'member',
  active boolean not null default true, token_version integer not null default 1,
  created_at timestamptz not null default now(), unique(organization_id,email)
);
create index if not exists users_email_idx on users(email);
create table if not exists locations (
  id text primary key, organization_id text not null references organizations(id),
  name text not null, active boolean not null default true, unique(organization_id,name)
);
create table if not exists customers (
  id text primary key, organization_id text not null references organizations(id), location_id text references locations(id),
  external_id text, name text not null, email text, phone text, created_at timestamptz not null default now(),
  unique(organization_id,external_id)
);
create table if not exists leads (
  id text primary key, organization_id text not null references organizations(id), customer_id text references customers(id),
  external_id text, source text not null default 'unknown', status text not null default 'new', value numeric(14,2) not null default 0,
  created_at timestamptz not null default now(), last_contact_at timestamptz, quote_sent_at timestamptz,
  unique(organization_id,external_id)
);
create table if not exists appointments (
  id text primary key, organization_id text not null references organizations(id), customer_id text references customers(id), lead_id text references leads(id),
  status text not null, value numeric(14,2) not null default 0, scheduled_at timestamptz not null, cancelled_at timestamptz
);
create table if not exists transactions (
  id text primary key, organization_id text not null references organizations(id), customer_id text references customers(id),
  external_id text, amount numeric(14,2) not null, status text not null, paid_at timestamptz not null, source_opportunity_id text,
  unique(organization_id,external_id)
);
create table if not exists payments (
  id text primary key, organization_id text not null references organizations(id), customer_id text references customers(id),
  external_id text, amount numeric(14,2) not null, status text not null, failed_at timestamptz not null,
  unique(organization_id,external_id)
);
create table if not exists reviews (
  id text primary key, organization_id text not null references organizations(id), customer_id text references customers(id),
  external_id text, rating integer not null, text_content text not null default '', topic text, severity text, created_at timestamptz not null default now(),
  unique(organization_id,external_id)
);
create table if not exists channel_consents (
  id text primary key, organization_id text not null references organizations(id), customer_id text not null references customers(id),
  channel text not null, allowed boolean not null default false, source text not null default 'unknown', updated_at timestamptz not null default now(),
  unique(organization_id,customer_id,channel)
);
create table if not exists revenue_opportunities (
  id text primary key, organization_id text not null references organizations(id), location_id text references locations(id),
  customer_id text references customers(id), lead_id text references leads(id), detection_key text not null, type text not null,
  status text not null default 'open', reason text not null, severity text not null, estimated_value numeric(14,2) not null,
  probability numeric(7,5) not null, expected_value numeric(14,2) not null, confidence numeric(7,5) not null,
  detected_at timestamptz not null default now(), recommended_action text, attribution_status text not null default 'estimated',
  expires_at timestamptz, version integer not null default 1, unique(organization_id,detection_key)
);
create index if not exists opp_org_status_value_idx on revenue_opportunities(organization_id,status,expected_value desc);
create table if not exists opportunity_events (
  id bigserial primary key, organization_id text not null references organizations(id), opportunity_id text not null references revenue_opportunities(id),
  event_type text not null, actor text not null, metadata_json jsonb not null default '{}', created_at timestamptz not null default now()
);
create index if not exists opp_events_opp_time_idx on opportunity_events(opportunity_id,created_at desc);
create table if not exists recovery_plans (
  id text primary key, organization_id text not null references organizations(id), opportunity_id text not null references revenue_opportunities(id),
  objective text not null, channel text not null, steps_json jsonb not null, status text not null default 'ready', approval_required boolean not null default true,
  created_at timestamptz not null default now()
);
create table if not exists recovery_actions (
  id bigserial primary key, organization_id text not null references organizations(id), opportunity_id text not null references revenue_opportunities(id),
  action_type text not null, channel text not null, status text not null default 'pending', payload_json jsonb not null,
  idempotency_key text not null unique, created_at timestamptz not null default now(), executed_at timestamptz
);
create table if not exists attribution_events (
  id bigserial primary key, organization_id text not null references organizations(id), opportunity_id text not null references revenue_opportunities(id),
  event_type text not null, event_time timestamptz not null default now(), external_id text, metadata_json jsonb not null default '{}'
);
create table if not exists audit_logs (
  id bigserial primary key, organization_id text references organizations(id), actor_user_id text, action text not null,
  resource_type text not null, resource_id text, metadata_json jsonb not null default '{}', created_at timestamptz not null default now()
);
create table if not exists integrations (
  id text primary key, organization_id text not null references organizations(id), provider text not null,
  status text not null default 'not_configured', credentials_ref text, last_sync_at timestamptz,
  unique(organization_id,provider)
);
create table if not exists webhook_events (
  id bigserial primary key, organization_id text not null references organizations(id), external_id text not null,
  event_type text not null, payload_hash text not null, status text not null default 'received', received_at timestamptz not null default now(),
  unique(organization_id,external_id)
);

-- Defense-in-depth tenant isolation. The API sets app.current_org per authenticated request.
create or replace function rros_current_org() returns text language sql stable as $$ select nullif(current_setting('app.current_org', true),'') $$;

-- Login needs a narrowly scoped SECURITY DEFINER lookup because no tenant context
-- exists before credentials are verified. The function exposes only fields required
-- for password verification and never exposes a general-purpose cross-tenant query.
create or replace function rros_auth_lookup(p_email text, p_org_slug text default null)
returns table(user_id text, organization_id text, password_hash text, active boolean, token_version integer)
language sql security definer set search_path = public as $$
  select u.id, u.organization_id, u.password_hash, u.active, u.token_version
  from users u join organizations o on o.id=u.organization_id
  where u.email=lower(trim(p_email)) and u.active=true and o.active=true
    and (p_org_slug is null or o.slug=p_org_slug)
  limit 2
$$;
revoke all on function rros_auth_lookup(text,text) from public;


alter table organizations enable row level security;
alter table users enable row level security;
alter table locations enable row level security;
alter table customers enable row level security;
alter table leads enable row level security;
alter table appointments enable row level security;
alter table transactions enable row level security;
alter table payments enable row level security;
alter table reviews enable row level security;
alter table channel_consents enable row level security;
alter table revenue_opportunities enable row level security;
alter table opportunity_events enable row level security;
alter table recovery_plans enable row level security;
alter table recovery_actions enable row level security;
alter table attribution_events enable row level security;
alter table audit_logs enable row level security;
alter table integrations enable row level security;
alter table webhook_events enable row level security;

-- Policies are created only if absent so this migration is repeatable.
do $$
declare t text;
begin
  foreach t in array array['users','locations','customers','leads','appointments','transactions','payments','reviews','channel_consents','revenue_opportunities','opportunity_events','recovery_plans','recovery_actions','attribution_events','integrations','webhook_events'] loop
    execute format('drop policy if exists rros_tenant_policy on %I',t);
    execute format('create policy rros_tenant_policy on %I using (organization_id = rros_current_org()) with check (organization_id = rros_current_org())',t);
  end loop;
  drop policy if exists rros_org_policy on organizations;
  create policy rros_org_policy on organizations using (id = rros_current_org()) with check (id = rros_current_org());
  drop policy if exists rros_audit_policy on audit_logs;
  create policy rros_audit_policy on audit_logs using (organization_id = rros_current_org() or organization_id is null) with check (organization_id = rros_current_org() or organization_id is null);
end $$;
