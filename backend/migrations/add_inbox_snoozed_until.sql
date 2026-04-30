-- Add `snoozed_until` to inbox_items for the Stagnation Monitor /
-- "Buried Task" reminder feature. Run once in Supabase SQL Editor —
-- additive, safe to re-run.
--
-- Behavior:
--   snoozed_until IS NULL  → not snoozed (default for every existing row)
--   snoozed_until > now()  → user clicked Snooze; hide from stale list
--                            and CEO check-in until this timestamp
--   snoozed_until < now()  → snooze expired; row reappears as stale
--
-- The Stagnation Monitor reads this column and excludes any row whose
-- snooze hasn't expired. There's no separate "is_snoozed" boolean —
-- the timestamp itself encodes both the state and the wake-up time.

alter table public.inbox_items
  add column if not exists snoozed_until timestamptz;

-- Partial index for the stagnation query: only rows that are actually
-- snoozed get indexed, keeping the index small. The stale-items query
-- filters with `(snoozed_until is null or snoozed_until < now())` and
-- this index speeds up the second leg of that OR.
create index if not exists inbox_items_snoozed_until_idx
  on public.inbox_items (snoozed_until)
  where snoozed_until is not null;
