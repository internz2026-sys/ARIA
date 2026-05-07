-- marketing_reports — persisted "State of the Union" / snapshot reports
-- generated on-demand from the Reports tab. Each row is one report card
-- in the UI, with the agent who wrote it, an AI-written summary, the
-- full markdown body, and references to chart PNGs already uploaded to
-- Supabase Storage by visualizer.upload_chart_to_storage.
--
-- Run once in Supabase SQL Editor.

create table if not exists public.marketing_reports (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null,
  -- Discriminator. Frontend filters / icon-maps by this.
  --   state_of_union    — 7-day cross-agent narrative (default Generate button)
  --   agent_productivity — bar chart of tasks per agent
  --   campaign_roi       — funnel chart from campaign_reports
  --   channel_spend      — pie chart across channels (future)
  --   daily_pulse        — 24-hour activity list (future)
  report_type text not null,
  -- Display name for the agent that authored the report. Plain string
  -- because some types (state_of_union, channel_spend) are CEO-authored
  -- composites that don't map onto AGENT_REGISTRY slugs cleanly.
  agent text,
  title text not null,
  -- Short (≤ 280 char) AI-written intro that fits on the card.
  summary text,
  -- Full narrative — markdown rendered in the report detail view.
  body_markdown text,
  -- Array of {url, type, title} objects referencing charts uploaded by
  -- visualizer.upload_chart_to_storage. Stored as jsonb so we can grow
  -- the chart count per report without schema changes.
  chart_urls jsonb not null default '[]'::jsonb,
  -- Raw aggregated counters the report was built from. Useful for the
  -- frontend to render small KPI tiles without re-parsing the markdown,
  -- and for future "compare to previous report" features.
  metrics jsonb not null default '{}'::jsonb,
  -- Reporting window the data covers. Both nullable so types like
  -- "state_of_union" (rolling 7d at generation time) and "agent_productivity"
  -- (since-account-creation) can both fit.
  period_start timestamptz,
  period_end timestamptz,
  created_at timestamptz not null default now()
);

create index if not exists marketing_reports_tenant_created
  on public.marketing_reports (tenant_id, created_at desc);

create index if not exists marketing_reports_tenant_type
  on public.marketing_reports (tenant_id, report_type);
