-- 2026_05_14_rls_profiles.sql — close the last RLS gap.
--
-- profiles was missed by 2026_05_14_rls_defense_in_depth.sql. With anon
-- key it leaks all rows: emails, role, banned_at, ban metadata. This
-- migration enables RLS and adds owner-only policies.
--
-- Apply as supabase_admin via Supabase Studio SQL editor (NOT
-- service_role — bypass would no-op the ENABLE).
--
-- profiles is keyed on `user_id` (NOT `id`) which equals auth.users.id.
-- The JWT's `sub` claim is the same UUID. Owner check is:
--   user_id::text = auth.jwt() ->> 'sub'
--
-- ADMIN escape hatch — banned_at / role admin lookups happen in the
-- backend via SERVICE_ROLE (which bypasses RLS) so no special admin
-- policy is needed here.
--
-- Idempotent: re-runnable.

alter table public.profiles enable row level security;

drop policy if exists profiles_self_select on public.profiles;
drop policy if exists profiles_self_insert on public.profiles;
drop policy if exists profiles_self_update on public.profiles;
drop policy if exists profiles_self_delete on public.profiles;

create policy profiles_self_select
  on public.profiles
  for select
  to authenticated
  using (user_id::text = auth.jwt() ->> 'sub');

create policy profiles_self_insert
  on public.profiles
  for insert
  to authenticated
  with check (user_id::text = auth.jwt() ->> 'sub');

create policy profiles_self_update
  on public.profiles
  for update
  to authenticated
  using (user_id::text = auth.jwt() ->> 'sub')
  with check (user_id::text = auth.jwt() ->> 'sub');

create policy profiles_self_delete
  on public.profiles
  for delete
  to authenticated
  using (user_id::text = auth.jwt() ->> 'sub');

-- End of migration. Re-runnable as written.
