-- Style memory — captures user edits to sub-agent drafts so agents can
-- learn a tenant's preferred style over time.
--
-- Populated by routers/inbox.py:update_inbox_item every time a
-- substantive edit lands on an inbox row (content or email_draft).
-- Consumed by BaseAgent.run() via asset_lookup.summarize_style_for_prompt
-- which appends the most recent N adjustments for this tenant+agent to
-- the system prompt as "user-preferred edits to emulate".
--
-- Apply with: supabase sql run < backend/sql/create_style_memory.sql
-- (or paste into the Supabase SQL editor).

create table if not exists style_adjustments (
    id           uuid primary key default gen_random_uuid(),
    tenant_id    uuid not null,
    agent        text not null,
    inbox_item_id uuid,
    original_content text not null,
    edited_content   text not null,
    diff_chars   integer not null default 0,
    created_at   timestamptz not null default now()
);

create index if not exists style_adjustments_tenant_agent_idx
    on style_adjustments (tenant_id, agent, created_at desc);

create index if not exists style_adjustments_inbox_item_idx
    on style_adjustments (inbox_item_id);


-- Approval cancellation reasons — extend inbox_items so the cancel-draft
-- flow captures an optional "why" from the user. Used by the same
-- feedback-into-prompt pipeline: future agent runs see "user cancelled
-- the last draft with reason: ..." and avoid the failure mode.

alter table inbox_items
    add column if not exists cancel_reason text;

create index if not exists inbox_items_cancel_reason_idx
    on inbox_items (tenant_id, agent)
    where cancel_reason is not null;
