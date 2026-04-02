-- Scheduled Tasks — time-based execution for emails, posts, campaigns, reminders
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,

    -- What to execute
    task_type TEXT NOT NULL CHECK (task_type IN (
        'send_email', 'publish_post', 'publish_campaign',
        'follow_up_task', 'reminder_task'
    )),
    title TEXT NOT NULL DEFAULT '',

    -- Link to related entity (inbox item, email thread, etc.)
    related_entity_type TEXT DEFAULT NULL,  -- 'inbox_item', 'email_thread', 'crm_contact', etc.
    related_entity_id UUID DEFAULT NULL,

    -- Execution payload (JSON with all data needed to execute)
    payload JSONB NOT NULL DEFAULT '{}',

    -- Scheduling
    scheduled_at TIMESTAMPTZ NOT NULL,
    timezone TEXT NOT NULL DEFAULT 'UTC',

    -- Status lifecycle
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'pending_approval', 'approved', 'scheduled',
        'running', 'sent', 'published', 'failed', 'cancelled'
    )),
    approval_status TEXT NOT NULL DEFAULT 'none' CHECK (approval_status IN (
        'none', 'pending', 'approved', 'rejected'
    )),

    -- Provenance
    created_by TEXT DEFAULT 'user',       -- 'user', 'ceo', 'content_writer', etc.
    triggered_by_agent TEXT DEFAULT NULL,  -- which agent created this

    -- Execution result
    execution_result JSONB DEFAULT NULL,
    executed_at TIMESTAMPTZ DEFAULT NULL,

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for the executor: find tasks ready to run
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_due
    ON scheduled_tasks (tenant_id, status, scheduled_at)
    WHERE status IN ('scheduled', 'approved');

-- Index for calendar queries
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_calendar
    ON scheduled_tasks (tenant_id, scheduled_at);
