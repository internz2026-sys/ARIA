-- Campaign entity + Campaign reports for Ad Strategist Agent
-- Manual Facebook Ads report ingestion workflow

-- ── Campaigns ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS campaigns (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id     uuid NOT NULL REFERENCES tenant_configs(tenant_id) ON DELETE CASCADE,
  campaign_name text NOT NULL,
  platform      text NOT NULL DEFAULT 'facebook',
  objective     text DEFAULT '',
  status        text NOT NULL DEFAULT 'active',
  date_range_start date,
  date_range_end   date,
  budget        numeric(12,2),
  notes         text DEFAULT '',
  tags          text[] DEFAULT '{}',

  -- Future-proofing for direct Meta Ads integration
  campaign_external_id   text,
  campaign_external_name text,
  owner_agent            text DEFAULT 'ad_strategist',
  source_type            text NOT NULL DEFAULT 'manual_upload',

  -- Denormalized latest report pointer for fast queries
  latest_report_id   uuid,
  latest_report_date timestamptz,

  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaigns_tenant ON campaigns(tenant_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_status ON campaigns(tenant_id, status);
CREATE INDEX IF NOT EXISTS idx_campaigns_platform ON campaigns(tenant_id, platform);

-- ── Campaign Reports ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS campaign_reports (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id        uuid NOT NULL REFERENCES tenant_configs(tenant_id) ON DELETE CASCADE,
  campaign_id      uuid NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
  source_file_name text DEFAULT '',
  source_type      text NOT NULL DEFAULT 'manual_upload',
  report_start_date date,
  report_end_date   date,
  uploaded_at      timestamptz NOT NULL DEFAULT now(),
  uploaded_by      text DEFAULT 'user',

  -- Parsing state
  parsed_status    text NOT NULL DEFAULT 'pending',  -- pending, parsed, failed
  raw_metrics_json jsonb DEFAULT '{}',

  -- AI analysis state
  ai_summary_status text NOT NULL DEFAULT 'pending', -- pending, generating, completed, failed
  ai_report_text    text DEFAULT '',
  ai_recommendations text DEFAULT '',

  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_campaign_reports_campaign ON campaign_reports(campaign_id);
CREATE INDEX IF NOT EXISTS idx_campaign_reports_tenant ON campaign_reports(tenant_id);
CREATE INDEX IF NOT EXISTS idx_campaign_reports_date ON campaign_reports(campaign_id, report_end_date DESC);
