-- all_tables_rls.sql — defense-in-depth tenant isolation for every
-- tenant-scoped table in the ARIA schema.
--
-- WHAT THIS DOES
--   Enables Row Level Security (RLS) on every table that holds per-tenant
--   data and installs a 4-policy set (SELECT / INSERT / UPDATE / DELETE)
--   keyed on the JWT email claim matched against tenant_configs.owner_email.
--   This is the canonical owner mapping used everywhere else in the
--   backend (see backend/auth.py:get_verified_tenant and
--   backend/server.py:join_tenant socket gate).
--
--   Modeled on backend/migrations/marketing_reports_rls.sql (the first
--   table to get the template). This file extends the same shape to the
--   rest of the tenant-scoped tables in one paste-and-run.
--
-- BACKEND BEHAVIOR IS UNCHANGED
--   The backend uses the SUPABASE_SERVICE_ROLE_KEY for all server-side
--   reads/writes. service_role BYPASSES RLS unconditionally, so enabling
--   these policies has zero effect on backend code paths. RLS only fires
--   for `anon` and `authenticated` JWTs (i.e. requests made directly from
--   the browser using the Supabase anon key). Today ARIA's frontend goes
--   through the FastAPI backend for everything that hits these tables, so
--   nothing breaks. If you add a direct browser-to-Supabase code path in
--   the future, RLS will gate it correctly out of the box.
--
-- HOW TO APPLY
--   Operator pastes this file into the Supabase SQL Editor once. It is
--   idempotent — every policy uses `drop policy if exists` + `create
--   policy`, and `alter table ... enable row level security` is a no-op
--   when already enabled. Safe to re-run on every deploy.
--
-- TABLES COVERED
--   - tenant_configs            (predicate keys directly on owner_email — no subquery)
--   - agent_logs                (predicate via tenant_configs subquery)
--   - inbox_items               (predicate via tenant_configs subquery)
--   - email_threads             (predicate via tenant_configs subquery)
--   - email_messages            (predicate via tenant_configs subquery)
--   - crm_contacts              (predicate via tenant_configs subquery)
--   - crm_companies             (predicate via tenant_configs subquery)
--   - crm_deals                 (predicate via tenant_configs subquery)
--   - crm_activities            (predicate via tenant_configs subquery)
--   - campaigns                 (predicate via tenant_configs subquery)
--   - campaign_reports          (predicate via tenant_configs subquery)
--   - notifications             (predicate via tenant_configs subquery)
--   - tasks                     (predicate via tenant_configs subquery)
--
-- TABLES SKIPPED
--   - profiles                  (skipped — not tenant-scoped, keyed on user_id)
--   - onboarding_drafts         (skipped — not tenant-scoped, keyed on user_id;
--                                already JWT-bound at the application layer
--                                via /api/onboarding/save-draft)
--   - marketing_reports         (skipped — already covered by
--                                marketing_reports_rls.sql, do not re-touch)
--
-- NOTES FOR FUTURE MAINTAINERS
--   - If ARIA migrates to multi-user-per-tenant, replace every inner
--     SELECT against tenant_configs with a join through a tenant_members
--     table. The shape of the predicate stays the same.
--   - service_role bypasses RLS unconditionally. If you ever switch the
--     backend off the service_role key, every server-side read will start
--     returning zero rows under RLS — that is a feature, not a bug.
--   - Do NOT add policies for `auth.uid()` against tenant_configs.user_id
--     because that column does not exist. The canonical mapping is
--     `auth.jwt() ->> 'email'` against `tenant_configs.owner_email`.

-- ─────────────────────────────────────────────────────────────────────────
-- 1. tenant_configs — the owner table itself.
--    Predicate is direct (no subquery): a row is yours iff its owner_email
--    matches the JWT email claim.
-- ─────────────────────────────────────────────────────────────────────────
alter table public.tenant_configs enable row level security;

drop policy if exists tenant_configs_owner_select on public.tenant_configs;
drop policy if exists tenant_configs_owner_insert on public.tenant_configs;
drop policy if exists tenant_configs_owner_update on public.tenant_configs;
drop policy if exists tenant_configs_owner_delete on public.tenant_configs;

create policy tenant_configs_owner_select
  on public.tenant_configs
  for select
  to authenticated
  using (lower(owner_email) = lower(auth.jwt() ->> 'email'));

create policy tenant_configs_owner_insert
  on public.tenant_configs
  for insert
  to authenticated
  with check (lower(owner_email) = lower(auth.jwt() ->> 'email'));

create policy tenant_configs_owner_update
  on public.tenant_configs
  for update
  to authenticated
  using (lower(owner_email) = lower(auth.jwt() ->> 'email'))
  with check (lower(owner_email) = lower(auth.jwt() ->> 'email'));

create policy tenant_configs_owner_delete
  on public.tenant_configs
  for delete
  to authenticated
  using (lower(owner_email) = lower(auth.jwt() ->> 'email'));


-- ─────────────────────────────────────────────────────────────────────────
-- 2. agent_logs
-- ─────────────────────────────────────────────────────────────────────────
alter table public.agent_logs enable row level security;

drop policy if exists agent_logs_tenant_select on public.agent_logs;
drop policy if exists agent_logs_tenant_insert on public.agent_logs;
drop policy if exists agent_logs_tenant_update on public.agent_logs;
drop policy if exists agent_logs_tenant_delete on public.agent_logs;

create policy agent_logs_tenant_select
  on public.agent_logs
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy agent_logs_tenant_insert
  on public.agent_logs
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy agent_logs_tenant_update
  on public.agent_logs
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy agent_logs_tenant_delete
  on public.agent_logs
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 3. inbox_items
-- ─────────────────────────────────────────────────────────────────────────
alter table public.inbox_items enable row level security;

drop policy if exists inbox_items_tenant_select on public.inbox_items;
drop policy if exists inbox_items_tenant_insert on public.inbox_items;
drop policy if exists inbox_items_tenant_update on public.inbox_items;
drop policy if exists inbox_items_tenant_delete on public.inbox_items;

create policy inbox_items_tenant_select
  on public.inbox_items
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy inbox_items_tenant_insert
  on public.inbox_items
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy inbox_items_tenant_update
  on public.inbox_items
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy inbox_items_tenant_delete
  on public.inbox_items
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 4. email_threads
-- ─────────────────────────────────────────────────────────────────────────
alter table public.email_threads enable row level security;

drop policy if exists email_threads_tenant_select on public.email_threads;
drop policy if exists email_threads_tenant_insert on public.email_threads;
drop policy if exists email_threads_tenant_update on public.email_threads;
drop policy if exists email_threads_tenant_delete on public.email_threads;

create policy email_threads_tenant_select
  on public.email_threads
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy email_threads_tenant_insert
  on public.email_threads
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy email_threads_tenant_update
  on public.email_threads
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy email_threads_tenant_delete
  on public.email_threads
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 5. email_messages
-- ─────────────────────────────────────────────────────────────────────────
alter table public.email_messages enable row level security;

drop policy if exists email_messages_tenant_select on public.email_messages;
drop policy if exists email_messages_tenant_insert on public.email_messages;
drop policy if exists email_messages_tenant_update on public.email_messages;
drop policy if exists email_messages_tenant_delete on public.email_messages;

create policy email_messages_tenant_select
  on public.email_messages
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy email_messages_tenant_insert
  on public.email_messages
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy email_messages_tenant_update
  on public.email_messages
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy email_messages_tenant_delete
  on public.email_messages
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 6. crm_contacts
-- ─────────────────────────────────────────────────────────────────────────
alter table public.crm_contacts enable row level security;

drop policy if exists crm_contacts_tenant_select on public.crm_contacts;
drop policy if exists crm_contacts_tenant_insert on public.crm_contacts;
drop policy if exists crm_contacts_tenant_update on public.crm_contacts;
drop policy if exists crm_contacts_tenant_delete on public.crm_contacts;

create policy crm_contacts_tenant_select
  on public.crm_contacts
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_contacts_tenant_insert
  on public.crm_contacts
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_contacts_tenant_update
  on public.crm_contacts
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_contacts_tenant_delete
  on public.crm_contacts
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 7. crm_companies
-- ─────────────────────────────────────────────────────────────────────────
alter table public.crm_companies enable row level security;

drop policy if exists crm_companies_tenant_select on public.crm_companies;
drop policy if exists crm_companies_tenant_insert on public.crm_companies;
drop policy if exists crm_companies_tenant_update on public.crm_companies;
drop policy if exists crm_companies_tenant_delete on public.crm_companies;

create policy crm_companies_tenant_select
  on public.crm_companies
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_companies_tenant_insert
  on public.crm_companies
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_companies_tenant_update
  on public.crm_companies
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_companies_tenant_delete
  on public.crm_companies
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 8. crm_deals
-- ─────────────────────────────────────────────────────────────────────────
alter table public.crm_deals enable row level security;

drop policy if exists crm_deals_tenant_select on public.crm_deals;
drop policy if exists crm_deals_tenant_insert on public.crm_deals;
drop policy if exists crm_deals_tenant_update on public.crm_deals;
drop policy if exists crm_deals_tenant_delete on public.crm_deals;

create policy crm_deals_tenant_select
  on public.crm_deals
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_deals_tenant_insert
  on public.crm_deals
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_deals_tenant_update
  on public.crm_deals
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_deals_tenant_delete
  on public.crm_deals
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 9. crm_activities
-- ─────────────────────────────────────────────────────────────────────────
alter table public.crm_activities enable row level security;

drop policy if exists crm_activities_tenant_select on public.crm_activities;
drop policy if exists crm_activities_tenant_insert on public.crm_activities;
drop policy if exists crm_activities_tenant_update on public.crm_activities;
drop policy if exists crm_activities_tenant_delete on public.crm_activities;

create policy crm_activities_tenant_select
  on public.crm_activities
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_activities_tenant_insert
  on public.crm_activities
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_activities_tenant_update
  on public.crm_activities
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy crm_activities_tenant_delete
  on public.crm_activities
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 10. campaigns
-- ─────────────────────────────────────────────────────────────────────────
alter table public.campaigns enable row level security;

drop policy if exists campaigns_tenant_select on public.campaigns;
drop policy if exists campaigns_tenant_insert on public.campaigns;
drop policy if exists campaigns_tenant_update on public.campaigns;
drop policy if exists campaigns_tenant_delete on public.campaigns;

create policy campaigns_tenant_select
  on public.campaigns
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy campaigns_tenant_insert
  on public.campaigns
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy campaigns_tenant_update
  on public.campaigns
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy campaigns_tenant_delete
  on public.campaigns
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 11. campaign_reports
-- ─────────────────────────────────────────────────────────────────────────
alter table public.campaign_reports enable row level security;

drop policy if exists campaign_reports_tenant_select on public.campaign_reports;
drop policy if exists campaign_reports_tenant_insert on public.campaign_reports;
drop policy if exists campaign_reports_tenant_update on public.campaign_reports;
drop policy if exists campaign_reports_tenant_delete on public.campaign_reports;

create policy campaign_reports_tenant_select
  on public.campaign_reports
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy campaign_reports_tenant_insert
  on public.campaign_reports
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy campaign_reports_tenant_update
  on public.campaign_reports
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy campaign_reports_tenant_delete
  on public.campaign_reports
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 12. notifications
-- ─────────────────────────────────────────────────────────────────────────
alter table public.notifications enable row level security;

drop policy if exists notifications_tenant_select on public.notifications;
drop policy if exists notifications_tenant_insert on public.notifications;
drop policy if exists notifications_tenant_update on public.notifications;
drop policy if exists notifications_tenant_delete on public.notifications;

create policy notifications_tenant_select
  on public.notifications
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy notifications_tenant_insert
  on public.notifications
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy notifications_tenant_update
  on public.notifications
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy notifications_tenant_delete
  on public.notifications
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 13. tasks
-- ─────────────────────────────────────────────────────────────────────────
alter table public.tasks enable row level security;

drop policy if exists tasks_tenant_select on public.tasks;
drop policy if exists tasks_tenant_insert on public.tasks;
drop policy if exists tasks_tenant_update on public.tasks;
drop policy if exists tasks_tenant_delete on public.tasks;

create policy tasks_tenant_select
  on public.tasks
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy tasks_tenant_insert
  on public.tasks
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy tasks_tenant_update
  on public.tasks
  for update
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy tasks_tenant_delete
  on public.tasks
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

-- End of migration. Re-runnable as written.
