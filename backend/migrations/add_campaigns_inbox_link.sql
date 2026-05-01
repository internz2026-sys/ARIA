-- Add inbox/task link columns + metadata JSONB to the campaigns table
-- so finalized Ad Strategist deliverables can be mirrored from the
-- inbox into Campaigns (in addition to Projects). Additive, idempotent.
--
-- Why:
--   When the Ad Strategist finalizes a campaign brief, the deliverable
--   currently lands in:
--     1. inbox_items  — copy-paste content for the user
--     2. tasks        — Projects page tracker (via inbox_item_id link)
--   We're adding a third mirror so the Campaigns page Copy-Paste tab
--   can render the same deliverable as a queued draft. Without
--   inbox_item_id on campaigns, the frontend has no way to look up
--   "the campaign row that came from this inbox item" for idempotency
--   or to deep-link the Review button.
--
--   `task_id` is the optional back-pointer to the Projects mirror, so
--   the three rows form a triangle (inbox <-> tasks <-> campaigns)
--   that the UI can navigate without N+1 lookups.
--
--   `metadata` JSONB is a forward-compatible escape hatch for fields
--   like projected_budget / campaign_objective extracted from the
--   agent's markdown — keeps us from chasing new migrations every
--   time the brief format gains a field.

alter table public.campaigns
  add column if not exists inbox_item_id uuid,
  add column if not exists task_id uuid,
  add column if not exists metadata jsonb default '{}'::jsonb;

-- One campaigns row per inbox item — partial unique index so legacy
-- campaigns rows (NULL inbox_item_id) stay valid while still catching
-- duplicate inserts when two paths fire for the same Ad Strategist
-- deliverable (skill-curl + watcher placeholder, agent retries, etc.).
create unique index if not exists campaigns_inbox_item_id_unique
  on public.campaigns (inbox_item_id)
  where inbox_item_id is not null;
