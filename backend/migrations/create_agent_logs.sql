-- agent_logs — append-only record of every agent dispatch
--
-- Written to by `log_agent_action()` in backend/orchestrator.py at the end
-- of every Paperclip + local agent run. Powers:
--
--   * Reports → Agent Productivity bar chart (tasks per agent / 7d)
--   * Reports → State of the Union "tasks completed" counter
--   * Reports → Daily Pulse 24-hour activity list
--   * Dashboard → Virtual Office "Recent Activity" widget
--   * CEO chat → read_agent_logs action for in-conversation lookups
--
-- The write is wrapped in try/except so missing-table errors are silent
-- — that's why the table being absent has gone unnoticed (every report
-- silently returns 0 tasks). This migration creates the schema the
-- existing readers + writer already expect.
--
-- Run once in Supabase SQL Editor — additive, safe to re-run.

create table if not exists public.agent_logs (
  id uuid primary key default gen_random_uuid(),

  -- Tenant scope. Every read/write filters on this so the index below
  -- carries it as the leading column.
  tenant_id uuid not null,

  -- Slug of the agent that ran (`ceo`, `content_writer`, `email_marketer`,
  -- `social_manager`, `ad_strategist`, `media`). Plain text rather than
  -- enum so adding a new agent doesn't require a migration.
  agent_name text not null,

  -- Short string describing what was attempted, e.g. "ran content_writer
  -- task: blog post draft". Free-form for the activity feed.
  action text,

  -- JSON blob of whatever the agent returned. inbox_item_id, paperclip
  -- issue id, error details, etc. Reports mostly ignore this; the CEO
  -- chat read_agent_logs action surfaces it for conversational lookups.
  result jsonb not null default '{}'::jsonb,

  -- Outcome — `completed`, `completed_with_warning`, `failed`, `skipped`.
  -- Reports filter to completed/completed_with_warning when counting
  -- "tasks done" so the productivity chart doesn't include attempts
  -- that errored.
  status text not null default 'completed',

  -- When the agent finished. Reports use rolling-window queries
  -- (`timestamp >= now() - interval '7 days'`) so this is the dimension
  -- the index below leads on after tenant_id.
  timestamp timestamptz not null default now()
);

-- Primary query pattern: per-tenant, ordered by recency. Reports +
-- activity feed both use `(tenant_id = X) and (timestamp >= Y)
-- order by timestamp desc`. desc index lets the planner stop scanning
-- once it crosses the window.
create index if not exists agent_logs_tenant_timestamp_idx
  on public.agent_logs (tenant_id, timestamp desc);

-- Per-agent rollup index for the productivity chart's
-- group-by-agent_name aggregation.
create index if not exists agent_logs_tenant_agent_idx
  on public.agent_logs (tenant_id, agent_name);
