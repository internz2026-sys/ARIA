-- add_tenant_plan — pricing-tier column on tenant_configs
--
-- ARIA's pricing page advertises 4 tiers (free, starter, growth, scale) but
-- the codebase has been treating every tenant as unlimited. This migration
-- adds the `plan` column that backend/services/plan_quotas.py reads to
-- enforce per-plan caps in dispatch_agent() and the inbox CREATE route:
--
--   free      — 3 content pieces/mo, 0 campaign plans, email_sequences disabled
--   starter   — 10 content pieces/mo, 1 campaign plan/mo, email_sequences disabled
--   growth    — 30 content pieces/mo, 3 campaign plans/mo, email_sequences enabled
--   scale     — unlimited content + campaigns, email_sequences enabled
--
-- Backfill: existing rows get plan='scale' so alpha users aren't suddenly
-- throttled by a new gate they never opted into. NEW signups (and the
-- TenantConfig pydantic default) start on 'free' — they're the ones the
-- "try before you buy" tier exists for.
--
-- The column is intentionally plain text + a CHECK constraint rather than
-- a Postgres ENUM. Adding tiers later (e.g. an annual-billed "team" plan)
-- is then a one-line constraint swap rather than ALTER TYPE gymnastics.
--
-- Run once in Supabase SQL Editor — additive, safe to re-run.

alter table public.tenant_configs
  add column if not exists plan text not null default 'free';

-- Add the CHECK constraint separately so re-running the migration on a DB
-- that already has the column (without a constraint) still applies it.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'tenant_configs_plan_check'
      and conrelid = 'public.tenant_configs'::regclass
  ) then
    alter table public.tenant_configs
      add constraint tenant_configs_plan_check
      check (plan in ('free', 'starter', 'growth', 'scale'));
  end if;
end$$;

-- Backfill: any pre-existing row predates the gate, so unmark it as 'free'
-- (which would suddenly cap working alpha tenants at 3 content pieces a
-- month) and put it on 'scale' instead. New signups arriving AFTER this
-- migration land on 'free' via the column default + pydantic schema.
update public.tenant_configs
  set plan = 'scale'
  where plan = 'free'
    and created_at < now();

-- Index for usage queries: plan_quotas.check_quota() looks up the tenant's
-- plan on every dispatch. The existing tenant_configs primary key already
-- covers the tenant_id lookup, but a small index on plan helps the
-- admin/reporting queries that group tenants by tier.
create index if not exists tenant_configs_plan_idx
  on public.tenant_configs (plan);
