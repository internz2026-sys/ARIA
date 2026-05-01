-- Add project-task columns: link tasks to inbox items + carry a
-- separate display title + a metadata JSONB for per-agent extras
-- (campaign objective, projected budget, etc.). Additive, safe to
-- re-run.
--
-- Why:
--   The Projects page surfaces tasks one-row-per-deliverable (e.g. an
--   Ad Strategist campaign). Without inbox_item_id, the "Review"
--   button has nowhere to deep-link to. Without `title`, the row
--   shows the raw task description instead of the AI-generated
--   campaign title. `metadata` is a forward-compatible escape hatch
--   so per-agent fields don't require new migrations every time.

alter table public.tasks
  add column if not exists inbox_item_id uuid,
  add column if not exists title text,
  add column if not exists metadata jsonb default '{}'::jsonb;

-- One task per inbox item — partial unique index so legacy tasks
-- with NULL inbox_item_id stay valid. Catches duplicate inserts when
-- two inbox events fire for the same campaign (e.g. agent retries).
create unique index if not exists tasks_inbox_item_id_unique
  on public.tasks (inbox_item_id)
  where inbox_item_id is not null;
