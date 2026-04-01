-- Agent status persistence for Virtual Office
-- Stores current agent state so it survives page navigation

CREATE TABLE IF NOT EXISTS agent_status (
  tenant_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'idle',
  current_task TEXT DEFAULT '',
  action TEXT DEFAULT '',
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  PRIMARY KEY (tenant_id, agent_id)
);

-- Index for fast lookups by tenant
CREATE INDEX IF NOT EXISTS idx_agent_status_tenant ON agent_status(tenant_id);
