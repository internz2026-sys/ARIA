-- Add `status` column to profiles for usage suspension / account pausing.
-- Run once in Supabase SQL Editor — additive, safe to re-run.
--
-- Status values:
--   active     — normal use (default)
--   paused     — soft lock: blocked from expensive agent actions, can still
--                read dashboard/inbox/history. Manual admin trigger for v1.
--   suspended  — harder lock (reserved for future automated abuse/billing
--                enforcement). Same gate logic as paused for now; the
--                separation lets us evolve the messaging later without
--                another migration.

alter table public.profiles
  add column if not exists status text not null default 'active';

-- Add the check constraint separately so re-running the migration on a
-- DB that already has the column (without a constraint) still applies it.
do $$
begin
  if not exists (
    select 1 from pg_constraint
    where conname = 'profiles_status_check'
      and conrelid = 'public.profiles'::regclass
  ) then
    alter table public.profiles
      add constraint profiles_status_check
      check (status in ('active', 'paused', 'suspended'));
  end if;
end$$;

create index if not exists profiles_status_idx on public.profiles (status);

-- Backfill any pre-existing NULLs (shouldn't exist with the NOT NULL DEFAULT
-- above, but defensive — covers the case where the column was added without
-- a default by a prior partial migration).
update public.profiles set status = 'active' where status is null;
