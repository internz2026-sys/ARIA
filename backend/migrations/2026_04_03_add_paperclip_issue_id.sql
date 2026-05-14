-- Add paperclip_issue_id column to inbox_items for efficient dedup in the poller.
-- Replaces the slow ilike() title scan with an exact indexed lookup.

ALTER TABLE inbox_items
  ADD COLUMN IF NOT EXISTS paperclip_issue_id text;

-- Index for the poller's "already processed?" check
CREATE INDEX IF NOT EXISTS idx_inbox_items_paperclip_issue_id
  ON inbox_items (paperclip_issue_id)
  WHERE paperclip_issue_id IS NOT NULL;
