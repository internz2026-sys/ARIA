-- marketing_reports RLS — defense-in-depth tenant isolation
--
-- Addresses HIGH audit finding: marketing_reports has no RLS policy. Today,
-- tenant isolation depends entirely on application-level `.eq("tenant_id", ...)`
-- filters in supabase-py (see backend/services/reports.py). If any future
-- endpoint forgets that filter, or if the anon key is ever swapped for the
-- service role on a code path, all tenants' AI narratives + chart URLs become
-- readable cross-tenant. This migration enables RLS so that even if a query
-- forgets the tenant_id filter, the database still gates access.
--
-- IMPORTANT — no existing ARIA table has RLS configured at the time of
-- writing (greps for "enable row level security" / "create policy" against
-- backend/migrations/ and backend/sql/ return zero matches). This migration
-- is therefore the first RLS template in the repo. The predicate links
-- through `tenant_configs.owner_email` because that is how ARIA already maps
-- auth users to tenants (see backend/server.py:1088 in `join_tenant` socket
-- gate, and `tenant_configs.owner_email` in tenant_schema.py:129). There is
-- NO `user_id` column on tenant_configs — do not try to use auth.uid()
-- against a non-existent column.
--
-- Assumptions:
--   1. The backend uses the service_role key (SUPABASE_SERVICE_ROLE_KEY) for
--      all server-side reads/writes. service_role BYPASSES RLS by default,
--      so this policy does NOT change backend behavior — it only locks down
--      anon / authenticated JWTs from the browser.
--   2. `auth.jwt() ->> 'email'` is populated for every authenticated user
--      (true for Supabase Auth email + OAuth flows).
--   3. `tenant_configs.owner_email` is the canonical owner mapping. If a
--      tenant has multiple users in the future, this predicate must be
--      widened (e.g. via a tenant_members join table).
--
-- Safe to re-run — uses IF EXISTS / DROP POLICY IF EXISTS guards.

-- 1. Enable RLS on the table. ALTER TABLE ... ENABLE is itself idempotent
--    (re-running on an already-enabled table is a no-op, no error).
alter table public.marketing_reports enable row level security;

-- 2. Drop existing policies first so we can re-run this migration cleanly
--    if the predicate ever changes.
drop policy if exists marketing_reports_tenant_select on public.marketing_reports;
drop policy if exists marketing_reports_tenant_insert on public.marketing_reports;
drop policy if exists marketing_reports_tenant_update on public.marketing_reports;
drop policy if exists marketing_reports_tenant_delete on public.marketing_reports;

-- 3. SELECT — a row is visible to a JWT only if its tenant_id belongs to a
--    tenant_configs row whose owner_email matches the JWT's email claim.
create policy marketing_reports_tenant_select
  on public.marketing_reports
  for select
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

-- 4. INSERT — same predicate, applied via WITH CHECK so a user can only
--    create reports for their own tenant.
create policy marketing_reports_tenant_insert
  on public.marketing_reports
  for insert
  to authenticated
  with check (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

-- 5. UPDATE — gates both the existing row (USING) and the post-update row
--    (WITH CHECK) so a user cannot pivot a row out of their own tenant.
create policy marketing_reports_tenant_update
  on public.marketing_reports
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

-- 6. DELETE — same predicate. Backend service_role still deletes freely.
create policy marketing_reports_tenant_delete
  on public.marketing_reports
  for delete
  to authenticated
  using (
    tenant_id in (
      select tc.tenant_id
      from public.tenant_configs tc
      where lower(tc.owner_email) = lower(auth.jwt() ->> 'email')
    )
  );

-- Notes for the next person to touch this:
--   - If you migrate to a multi-user-per-tenant model, replace the inner
--     SELECT in each policy with a join against the new tenant_members table.
--   - To extend RLS to other tenant-scoped tables (campaigns, inbox_items,
--     content_library, etc.) copy this file as a template — same predicate
--     shape, just swap the table name.
--   - service_role bypasses RLS unconditionally. If you ever switch the
--     backend to use the anon key, every server-side read will start
--     returning zero rows under RLS — that is a feature, not a bug.
