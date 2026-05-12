-- Add `ban_reason` and `banned_until` columns to profiles for richer ban
-- metadata (see backend/services/profiles.py :: ban_user / unban_user and
-- backend/routers/admin.py :: admin_ban_user).
--
-- Companion to migrations/profiles_banned.sql which added `banned_at`.
-- Splitting this off into its own migration so existing deployments
-- (which already have `banned_at`) can be brought up to date by running
-- just this script in the Supabase SQL Editor.
--
-- Columns added:
--   * ban_reason text         -- optional human-readable reason for the
--                                ban. Shown on the /banned page so the
--                                user knows *why* they were locked out.
--                                Nullable: many bans don't carry a reason
--                                (legacy ban_user calls before this
--                                feature didn't persist one) and a NULL
--                                surface lets the frontend render a
--                                generic message.
--   * banned_until timestamptz -- computed end-of-ban timestamp at the
--                                moment the ban was placed (banned_at +
--                                duration). Stored separately from
--                                banned_at so a list view can render
--                                "Banned until 2026-12-31" without
--                                having to know the original duration.
--                                NULL means "indefinite" (Supabase's
--                                100yr sentinel ban is still computed
--                                but a NULL here is cheaper to detect
--                                than parsing the 100yr literal).
--
-- Run once in Supabase SQL Editor — additive, safe to re-run.

alter table public.profiles
  add column if not exists ban_reason text;

alter table public.profiles
  add column if not exists banned_until timestamptz;

-- Speeds up admin filters like "show users banned until after 2026-07-01"
-- and the cleanup job that lifts expired bans (when we ship one).
create index if not exists profiles_banned_until_idx on public.profiles (banned_until)
  where banned_until is not null;
