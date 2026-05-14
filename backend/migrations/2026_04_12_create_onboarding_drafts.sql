-- Onboarding Drafts — server-side persistence for in-progress onboarding
-- Solves the bug where users who clear localStorage / open onboarding in
-- a second tab / hard-refresh after a long idle would lose their 10-min
-- CEO conversation and have to restart from scratch.
--
-- The frontend mirrors the localStorage state to this table after every
-- successful extraction in /review, and tries to restore from this table
-- first on /select-agents mount before falling back to localStorage.
--
-- Keyed on user_id (Supabase auth uid) so each user has at most one
-- in-progress draft. Drafts are cleared by the frontend after successful
-- save-config (when the tenant_id is created).

CREATE TABLE IF NOT EXISTS onboarding_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Owner -- the Supabase auth user UID. Unique so we always have
    -- exactly one in-progress draft per user; subsequent saves UPSERT.
    user_id TEXT NOT NULL UNIQUE,

    -- The in-memory onboarding session id (FastAPI side). Stored so the
    -- frontend can attempt to resume the same session if the backend
    -- still has it; otherwise it falls through to using extracted_config.
    session_id TEXT,

    -- The fully-extracted config (output of /api/onboarding/extract-config).
    -- This is what gets passed to /api/onboarding/save-config-direct when
    -- the user finally clicks Launch.
    extracted_config JSONB NOT NULL DEFAULT '{}',

    -- Topics the user explicitly skipped during onboarding (so we don't
    -- prompt them again on edit-profile).
    skipped_topics JSONB DEFAULT NULL,

    -- Snapshot of the chat history at the moment of save -- optional but
    -- lets a future "resume conversation" feature pick up where we left off.
    conversation_history JSONB DEFAULT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lookup index (UNIQUE constraint already provides this, but listed for clarity)
CREATE INDEX IF NOT EXISTS idx_onboarding_drafts_user ON onboarding_drafts(user_id);

-- Auto-update updated_at on every UPSERT
CREATE OR REPLACE FUNCTION onboarding_drafts_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS onboarding_drafts_touch ON onboarding_drafts;
CREATE TRIGGER onboarding_drafts_touch
    BEFORE UPDATE ON onboarding_drafts
    FOR EACH ROW
    EXECUTE FUNCTION onboarding_drafts_touch_updated_at();
