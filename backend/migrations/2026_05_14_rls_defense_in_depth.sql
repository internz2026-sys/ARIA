-- 2026_05_14_rls_defense_in_depth.sql — enable RLS on the 5 most
-- sensitive tenant-owned tables in ARIA as defense-in-depth.
--
-- IMPORTANT — HOW TO APPLY
--   Apply via Supabase Studio SQL editor — DO NOT run as service_role
--   (the migration itself touches RLS metadata; service_role bypass would
--   no-op the ENABLE statements). Apply as supabase_admin or a superuser
--   session (the Supabase Studio SQL editor runs as supabase_admin by
--   default, which is what you want).
--
-- WHAT THIS DOES
--   Enables Row Level Security on five tables that hold per-tenant data
--   and installs a 4-policy set (SELECT / INSERT / UPDATE / DELETE) keyed
--   on `auth.jwt() ->> 'email'` matched against tenant_configs.owner_email
--   (the canonical owner mapping — see backend/auth.py:get_verified_tenant
--   and backend/server.py:join_tenant socket gate).
--
-- WHY
--   Defense in depth. The backend uses SUPABASE_SERVICE_ROLE_KEY (see
--   backend/config/loader.py:_get_supabase) which BYPASSES RLS
--   unconditionally — so these policies have ZERO effect on any current
--   server-side code path. They protect against:
--     1. Future code mistakes — e.g. forgetting a `.eq("tenant_id", x)`
--        filter in a new query. With RLS on, a missing filter under any
--        non-service_role caller would return zero rows instead of leaking
--        another tenant's data.
--     2. Compromised anon-key callers — if the anon key ever leaks or a
--        future direct-from-browser code path is added, RLS gates it out
--        of the box.
--
-- TABLES COVERED (in priority order from the security audit)
--   1. tenant_configs   (predicate keys directly on owner_email)
--   2. inbox_items      (predicate via tenant_configs subquery)
--   3. chat_messages    (predicate via chat_sessions -> tenant_configs;
--                        chat_messages has no direct tenant_id column —
--                        ownership is established through the session FK)
--   4. notifications    (predicate via tenant_configs subquery)
--   5. tasks            (predicate via tenant_configs subquery)
--
-- RELATIONSHIP TO all_tables_rls.sql
--   `backend/migrations/all_tables_rls.sql` already covers 4 of these 5
--   tables (tenant_configs, inbox_items, notifications, tasks). This file
--   re-emits identical policies for those four (safe because of the
--   `drop policy if exists` pattern) and adds the previously-missing
--   `chat_messages` coverage. Re-running either file is safe; the two
--   files are deliberately redundant so this audit-trail migration is
--   self-contained.
--
-- IDEMPOTENCY
--   Every policy is wrapped in `drop policy if exists ...; create policy
--   ...`. `alter table ... enable row level security` is a no-op when
--   already enabled. Safe to re-run on every deploy or on top of
--   all_tables_rls.sql.
--
-- ROLLBACK
--   To revert: `alter table public.<name> disable row level security;`
--   for each table. Policies become inert when RLS is disabled but stay
--   defined — drop them explicitly if you want a clean slate.

-- ─────────────────────────────────────────────────────────────────────────
-- 1. tenant_configs — the owner table itself.
--    Predicate is direct (no subquery): a row is yours iff its
--    owner_email matches the JWT email claim.
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
-- 2. inbox_items
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
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy inbox_items_tenant_insert
  on public.inbox_items
  for insert
  to authenticated
  with check (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy inbox_items_tenant_update
  on public.inbox_items
  for update
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy inbox_items_tenant_delete
  on public.inbox_items
  for delete
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 3. chat_messages — CEO chat history.
--    chat_messages has NO direct tenant_id column. Ownership is
--    established through chat_sessions.session_id -> chat_sessions.id,
--    then chat_sessions.tenant_id -> tenant_configs.tenant_id. The
--    predicate is a two-level subquery.
--
--    See backend/sql/create_chat_tables.sql for the schema.
--
--    NOTE: we also enable RLS + policies on chat_sessions so the
--    subquery referenced by chat_messages policies is itself protected.
--    Without RLS on chat_sessions, a non-service_role caller could SELECT
--    chat_sessions to discover session IDs belonging to other tenants
--    (even though they couldn't read the messages themselves).
-- ─────────────────────────────────────────────────────────────────────────
alter table public.chat_sessions enable row level security;

drop policy if exists chat_sessions_tenant_select on public.chat_sessions;
drop policy if exists chat_sessions_tenant_insert on public.chat_sessions;
drop policy if exists chat_sessions_tenant_update on public.chat_sessions;
drop policy if exists chat_sessions_tenant_delete on public.chat_sessions;

create policy chat_sessions_tenant_select
  on public.chat_sessions
  for select
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy chat_sessions_tenant_insert
  on public.chat_sessions
  for insert
  to authenticated
  with check (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy chat_sessions_tenant_update
  on public.chat_sessions
  for update
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy chat_sessions_tenant_delete
  on public.chat_sessions
  for delete
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

alter table public.chat_messages enable row level security;

drop policy if exists chat_messages_tenant_select on public.chat_messages;
drop policy if exists chat_messages_tenant_insert on public.chat_messages;
drop policy if exists chat_messages_tenant_update on public.chat_messages;
drop policy if exists chat_messages_tenant_delete on public.chat_messages;

create policy chat_messages_tenant_select
  on public.chat_messages
  for select
  to authenticated
  using (
    session_id in (
      select cs.id
      from public.chat_sessions cs
      join public.tenant_configs tc
        on tc.tenant_id::text = cs.tenant_id::text
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy chat_messages_tenant_insert
  on public.chat_messages
  for insert
  to authenticated
  with check (
    session_id in (
      select cs.id
      from public.chat_sessions cs
      join public.tenant_configs tc
        on tc.tenant_id::text = cs.tenant_id::text
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy chat_messages_tenant_update
  on public.chat_messages
  for update
  to authenticated
  using (
    session_id in (
      select cs.id
      from public.chat_sessions cs
      join public.tenant_configs tc
        on tc.tenant_id::text = cs.tenant_id::text
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    session_id in (
      select cs.id
      from public.chat_sessions cs
      join public.tenant_configs tc
        on tc.tenant_id::text = cs.tenant_id::text
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy chat_messages_tenant_delete
  on public.chat_messages
  for delete
  to authenticated
  using (
    session_id in (
      select cs.id
      from public.chat_sessions cs
      join public.tenant_configs tc
        on tc.tenant_id::text = cs.tenant_id::text
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 4. notifications
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
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy notifications_tenant_insert
  on public.notifications
  for insert
  to authenticated
  with check (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy notifications_tenant_update
  on public.notifications
  for update
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy notifications_tenant_delete
  on public.notifications
  for delete
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );


-- ─────────────────────────────────────────────────────────────────────────
-- 5. tasks
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
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy tasks_tenant_insert
  on public.tasks
  for insert
  to authenticated
  with check (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy tasks_tenant_update
  on public.tasks
  for update
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  )
  with check (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

create policy tasks_tenant_delete
  on public.tasks
  for delete
  to authenticated
  using (
    tenant_id::text in (
      select tc.tenant_id::text
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

-- End of migration. Re-runnable as written.
