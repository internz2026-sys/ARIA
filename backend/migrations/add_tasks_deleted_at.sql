-- Add `deleted_at` to tasks for soft-delete + Trash tab.
-- Run once in Supabase SQL Editor — additive, safe to re-run.
--
-- Behavior:
--   deleted_at IS NULL  → live row (default for every existing task)
--   deleted_at NOT NULL → soft-deleted; hidden from main views,
--                         visible only in the Projects "Trash" tab
--
-- All read queries (list_tasks, Kanban, dashboard, Office widget) now
-- filter `deleted_at IS NULL` so soft-deleted rows disappear from
-- normal navigation. The DELETE handler updates deleted_at instead
-- of issuing a real DELETE; a future "permanent delete" or "restore"
-- endpoint operates on the deleted_at column.

alter table public.tasks
  add column if not exists deleted_at timestamptz;

-- Partial index only includes deleted rows so the index stays small
-- (we expect <5% of rows to be in trash at any given time, and the
-- main IS NULL filter benefits from skipping the index entirely).
create index if not exists tasks_deleted_at_idx
  on public.tasks (deleted_at)
  where deleted_at is not null;
