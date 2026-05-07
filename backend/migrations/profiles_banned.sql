-- Add `banned_at` column to profiles for the auth-layer ban feature
-- (see backend/services/profiles.py :: ban_user / unban_user).
--
-- Decision: separate `banned_at timestamptz` column rather than overloading
-- `role = 'banned'` because:
--   1. Role represents *what privileges the user has* — collapsing the
--      account-state axis (active / paused / suspended / banned) onto it
--      would force every existing role-check to grow a "and not banned"
--      clause. The existing pause/suspend feature already uses a separate
--      `status` column for the same reason.
--   2. We want to know *when* the ban was placed for the audit log + UI
--      ("Banned on 2026-05-07"). A boolean role-flip throws that away.
--   3. Supabase auth.users is the source of truth for whether login
--      actually works — `banned_at` is purely a denormalised hint so the
--      admin UI can render a Banned badge without calling the Auth Admin
--      API on every page load.
--
-- Run once in Supabase SQL Editor — additive, safe to re-run.

alter table public.profiles
  add column if not exists banned_at timestamptz;

-- Speeds up admin list filters like "show banned users".
create index if not exists profiles_banned_at_idx on public.profiles (banned_at)
  where banned_at is not null;
