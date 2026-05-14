-- Email threads — groups related inbound/outbound messages into conversations
CREATE TABLE IF NOT EXISTS public.email_threads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES public.tenant_configs(tenant_id) ON DELETE CASCADE,
  gmail_thread_id TEXT,                     -- Gmail API thread ID for matching
  contact_email TEXT NOT NULL,              -- The external party (lead/customer)
  subject TEXT DEFAULT '',
  status TEXT DEFAULT 'open',               -- open, awaiting_reply, replied, closed
  last_message_at TIMESTAMPTZ DEFAULT now(),
  inbox_item_id UUID,                       -- Link to the original inbox_item that started this thread
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_email_threads_tenant ON public.email_threads(tenant_id);
CREATE INDEX IF NOT EXISTS idx_email_threads_gmail_tid ON public.email_threads(gmail_thread_id);
CREATE INDEX IF NOT EXISTS idx_email_threads_contact ON public.email_threads(tenant_id, contact_email);

-- Email messages — individual messages within a thread
CREATE TABLE IF NOT EXISTS public.email_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id UUID NOT NULL REFERENCES public.email_threads(id) ON DELETE CASCADE,
  tenant_id UUID NOT NULL REFERENCES public.tenant_configs(tenant_id) ON DELETE CASCADE,
  gmail_message_id TEXT,                    -- Gmail API message ID (for dedup)
  direction TEXT NOT NULL,                  -- 'outbound' or 'inbound'
  sender TEXT NOT NULL,
  recipients TEXT NOT NULL,                 -- comma-separated
  subject TEXT DEFAULT '',
  text_body TEXT DEFAULT '',
  html_body TEXT DEFAULT '',
  preview_snippet TEXT DEFAULT '',
  message_timestamp TIMESTAMPTZ DEFAULT now(),
  approval_status TEXT DEFAULT 'none',      -- none, draft_pending_approval, approved, sent, failed
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_email_messages_thread ON public.email_messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_email_messages_tenant ON public.email_messages(tenant_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_email_messages_gmail_mid ON public.email_messages(gmail_message_id) WHERE gmail_message_id IS NOT NULL;
