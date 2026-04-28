-- profiles table: source of truth for ARIA user roles (RBAC)
-- Roles: 'user' (default), 'admin', 'super_admin'
-- Run once in Supabase SQL Editor.

create table if not exists public.profiles (
  user_id uuid primary key references auth.users(id) on delete cascade,
  email text,
  full_name text,
  role text not null default 'user' check (role in ('user', 'admin', 'super_admin')),
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);

create index if not exists profiles_role_idx on public.profiles (role);
create index if not exists profiles_email_idx on public.profiles (email);

-- Auto-bump updated_at on every UPDATE so the admin UI shows a live
-- "last changed" column without callers having to touch the field.
create or replace function public.profiles_set_updated_at()
returns trigger language plpgsql as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists profiles_updated_at on public.profiles;
create trigger profiles_updated_at
  before update on public.profiles
  for each row execute procedure public.profiles_set_updated_at();

-- Backfill: insert a default 'user' profile for every existing auth user
-- so the admin user table isn't empty on day one.
insert into public.profiles (user_id, email, full_name, role)
select id, email, raw_user_meta_data ->> 'full_name', 'user'
from auth.users
on conflict (user_id) do nothing;

-- Auto-create a default 'user' profile whenever a new auth.users row is
-- inserted (Google OAuth signup, email/password signup, magic link, etc).
-- Without this, manual email/password signups would never appear in the
-- /admin user table until an admin manually re-runs the backfill above.
-- Idempotent — `on conflict do nothing` skips if a profile already exists.
create or replace function public.profiles_handle_new_auth_user()
returns trigger language plpgsql security definer as $$
begin
  insert into public.profiles (user_id, email, full_name, role)
  values (
    new.id,
    new.email,
    new.raw_user_meta_data ->> 'full_name',
    'user'
  )
  on conflict (user_id) do nothing;
  return new;
end;
$$;

drop trigger if exists profiles_on_auth_user_insert on auth.users;
create trigger profiles_on_auth_user_insert
  after insert on auth.users
  for each row execute procedure public.profiles_handle_new_auth_user();

-- BOOTSTRAP YOUR FIRST SUPER_ADMIN:
-- After running the above, find your auth user's UUID at
-- Supabase Dashboard -> Authentication -> Users, then run:
--
--   update public.profiles set role = 'super_admin' where user_id = '<your-uuid>';
--
-- All subsequent role changes can be done from the ARIA /admin UI.
