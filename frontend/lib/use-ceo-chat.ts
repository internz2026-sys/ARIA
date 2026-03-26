// ---------------------------------------------------------------------------
// Shared CEO Chat context — single state instance across all consumers.
// Wrap the layout in <CeoChatProvider>, then call useCeoChat() anywhere.
// ---------------------------------------------------------------------------

"use client";

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from "react";
import { API_URL } from "./api";

// ---- Types ----------------------------------------------------------------

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  delegations?: { agent: string; task: string; priority: string }[];
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
  send: (text: string) => Promise<void>;
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

export function CeoChatProvider({ children }: { children: React.ReactNode }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [sending, setSending] = useState(false);
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
        const res = await fetch(`${API_URL}/api/ceo/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ session_id: sessionId, message: trimmed, tenant_id: getTenantId() }),
        });
        const data = await res.json();
        if (mountedRef.current) {
          setMessages((p) => [
            ...p,
            { role: "assistant", content: data.response || "Something went wrong.", delegations: data.delegations || [] },
          ]);
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

  const value: CeoChatState = { messages, sessions, sessionId, sending, send, switchSession, startNewChat, refreshSessions };

  return React.createElement(CeoChatContext.Provider, { value }, children);
}

// ---- Consumer hook --------------------------------------------------------

export function useCeoChat(): CeoChatState {
  const ctx = useContext(CeoChatContext);
  if (!ctx) throw new Error("useCeoChat must be used within <CeoChatProvider>");
  return ctx;
}
