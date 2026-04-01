// ---------------------------------------------------------------------------
// Shared CEO Chat context — single state instance across all consumers.
// Wrap the layout in <CeoChatProvider>, then call useCeoChat() anywhere.
// ---------------------------------------------------------------------------

"use client";

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from "react";
import { API_URL } from "./api";
import { supabase } from "./supabase";

// ---- Types ----------------------------------------------------------------

export interface PendingConfirmation {
  title: string;
  message: string;
  action: string;
  params: Record<string, any>;
  confirm_label: string;
  cancel_label: string;
  destructive: boolean;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  delegations?: { agent: string; task: string; priority: string }[];
  pending_confirmations?: PendingConfirmation[];
  action_results?: any[];
}

export interface ChatSession {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

interface CeoChatState {
  messages: ChatMessage[];
  sessions: ChatSession[];
  sessionId: string;
  sending: boolean;
  pendingConfirmation: PendingConfirmation | null;
  send: (text: string) => Promise<void>;
  confirmAction: () => Promise<void>;
  cancelAction: () => void;
  switchSession: (sid: string) => void;
  startNewChat: () => void;
  refreshSessions: () => void;
}

// ---- Shared session key ---------------------------------------------------

const SESSION_KEY = "aria_ceo_chat_active";

function getTenantId(): string {
  return (typeof window !== "undefined" && localStorage.getItem("aria_tenant_id")) || "";
}

function getOrCreateSessionId(): string {
  const existing = typeof window !== "undefined" ? localStorage.getItem(SESSION_KEY) : null;
  if (existing) return existing;
  const tid = getTenantId() || "anon";
  const sid = `chat_${tid}_${Date.now()}`;
  if (typeof window !== "undefined") localStorage.setItem(SESSION_KEY, sid);
  return sid;
}

// ---- Context --------------------------------------------------------------

const CeoChatContext = createContext<CeoChatState | null>(null);

async function getAuthHeaders(): Promise<Record<string, string>> {
  try {
    const { data: { session } } = await supabase.auth.getSession();
    if (session?.access_token) return { Authorization: `Bearer ${session.access_token}` };
  } catch {}
  return {};
}

export function CeoChatProvider({ children }: { children: React.ReactNode }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [sending, setSending] = useState(false);
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    setSessionId(getOrCreateSessionId());
    return () => { mountedRef.current = false; };
  }, []);

  // Load messages when session changes
  useEffect(() => {
    if (!sessionId) return;
    fetch(`${API_URL}/api/ceo/chat/${sessionId}/history`)
      .then((r) => r.json())
      .then((d) => { if (mountedRef.current) setMessages(d.messages || []); })
      .catch(() => { if (mountedRef.current) setMessages([]); });
  }, [sessionId]);

  const refreshSessions = useCallback(() => {
    const tid = getTenantId();
    if (!tid) return;
    fetch(`${API_URL}/api/ceo/chat/sessions/${tid}`)
      .then((r) => r.json())
      .then((d) => { if (mountedRef.current) setSessions(d.sessions || []); })
      .catch(() => {});
  }, []);

  useEffect(() => { refreshSessions(); }, [refreshSessions]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || sending) return;

      setMessages((p) => [...p, { role: "user", content: trimmed }]);
      setSending(true);

      try {
        const authHeaders = await getAuthHeaders();
        const res = await fetch(`${API_URL}/api/ceo/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders },
          body: JSON.stringify({ session_id: sessionId, message: trimmed, tenant_id: getTenantId() }),
        });
        const data = await res.json();
        if (mountedRef.current) {
          const msg: ChatMessage = {
            role: "assistant",
            content: data.response || "Something went wrong.",
            delegations: data.delegations || [],
            pending_confirmations: data.pending_confirmations || [],
            action_results: data.action_results || [],
          };
          setMessages((p) => [...p, msg]);

          // If there are pending confirmations, show the first one
          if (data.pending_confirmations?.length > 0) {
            setPendingConfirmation(data.pending_confirmations[0].confirmation);
          }

          refreshSessions();
        }
      } catch {
        if (mountedRef.current) {
          setMessages((p) => [...p, { role: "assistant", content: "Connection error — is the backend running?" }]);
        }
      }

      if (mountedRef.current) setSending(false);
    },
    [sessionId, sending, refreshSessions],
  );

  const confirmAction = useCallback(async () => {
    if (!pendingConfirmation) return;
    setSending(true);
    try {
      const authHeaders = await getAuthHeaders();
      const res = await fetch(`${API_URL}/api/ceo/${getTenantId()}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({
          action: pendingConfirmation.action,
          params: pendingConfirmation.params,
          confirmed: true,
        }),
      });
      const data = await res.json();
      if (mountedRef.current) {
        setMessages((p) => [...p, { role: "assistant", content: `Action completed: ${pendingConfirmation.action}` }]);
        setPendingConfirmation(null);
      }
    } catch (e: any) {
      if (mountedRef.current) {
        setMessages((p) => [...p, { role: "assistant", content: `Action failed: ${e?.message || "unknown error"}` }]);
        setPendingConfirmation(null);
      }
    }
    if (mountedRef.current) setSending(false);
  }, [pendingConfirmation]);

  const cancelAction = useCallback(() => {
    setPendingConfirmation(null);
    setMessages((p) => [...p, { role: "assistant", content: "Action cancelled." }]);
  }, []);

  const switchSession = useCallback((sid: string) => {
    localStorage.setItem(SESSION_KEY, sid);
    setSessionId(sid);
  }, []);

  const startNewChat = useCallback(() => {
    const tid = getTenantId() || "anon";
    const sid = `chat_${tid}_${Date.now()}`;
    localStorage.setItem(SESSION_KEY, sid);
    setSessionId(sid);
    setMessages([]);
  }, []);

  const value: CeoChatState = { messages, sessions, sessionId, sending, pendingConfirmation, send, confirmAction, cancelAction, switchSession, startNewChat, refreshSessions };

  return React.createElement(CeoChatContext.Provider, { value }, children);
}

// ---- Consumer hook --------------------------------------------------------

export function useCeoChat(): CeoChatState {
  const ctx = useContext(CeoChatContext);
  if (!ctx) throw new Error("useCeoChat must be used within <CeoChatProvider>");
  return ctx;
}
