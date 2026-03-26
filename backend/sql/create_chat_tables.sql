CREATE TABLE IF NOT EXISTS public.chat_sessions (
  id TEXT PRIMARY KEY,
  tenant_id UUID REFERENCES public.tenant_configs(tenant_id) ON DELETE CASCADE,
  title TEXT DEFAULT 'New chat',
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.chat_messages (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  session_id TEXT NOT NULL REFERENCES public.chat_sessions(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  delegations JSONB DEFAULT '[]',
  created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON public.chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_chat_sessions_tenant ON public.chat_sessions(tenant_id);
