// ---------------------------------------------------------------------------
// Shared CEO Chat context — single state instance across all consumers.
// Wrap the layout in <CeoChatProvider>, then call useCeoChat() anywhere.
// ---------------------------------------------------------------------------

"use client";

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from "react";
import { API_URL, getAuthHeaders, ceoChat as ceoChatApi } from "./api";

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
  cancel: () => void;
  confirmAction: () => Promise<void>;
  cancelAction: () => void;
  switchSession: (sid: string) => void;
  startNewChat: () => void;
  refreshSessions: () => void;
  /** Hard-delete a session + its messages. If `sid` is the current
   *  session the view resets to a fresh chat state. */
  deleteSession: (sid: string) => Promise<void>;
  /** Bulk-delete multiple sessions in one round-trip. Returns the
   *  count of sessions actually removed. If any of the deleted ids is
   *  the CURRENT session, the view resets to a fresh chat state. */
  deleteSessions: (ids: string[]) => Promise<number>;
}

// ---- Shared session key ---------------------------------------------------
// NOTE: key is intentionally suffixed `_v2` so any user with a stale session
// from before idle-timeout was added gets a one-shot fresh start on next load.
// Bump the suffix again if we ever need to force-reset all chat sessions.

const SESSION_KEY = "aria_ceo_chat_session_v2";
const SESSION_TS_KEY = "aria_ceo_chat_session_ts_v2";
const SESSION_IDLE_TIMEOUT_MS = 30 * 60 * 1000; // 30 minutes

function getTenantId(): string {
  return (typeof window !== "undefined" && localStorage.getItem("aria_tenant_id")) || "";
}

function makeSessionId(): string {
  const tid = getTenantId() || "anon";
  return `chat_${tid}_${Date.now()}`;
}

function touchSessionTimestamp() {
  if (typeof window === "undefined") return;
  localStorage.setItem(SESSION_TS_KEY, String(Date.now()));
}

function getOrCreateSessionId(): string {
  if (typeof window === "undefined") return makeSessionId();

  // Clean up the old key from previous versions so it can't bleed back in.
  localStorage.removeItem("aria_ceo_chat_active");

  const existing = localStorage.getItem(SESSION_KEY);
  const lastUsedRaw = localStorage.getItem(SESSION_TS_KEY);
  const lastUsed = lastUsedRaw ? parseInt(lastUsedRaw, 10) : 0;
  const isStale = !lastUsed || Date.now() - lastUsed > SESSION_IDLE_TIMEOUT_MS;

  if (existing && !isStale) {
    return existing;
  }

  // Either no session yet, or the previous one has been idle too long —
  // rotate to a fresh session so stale chat history can't influence the
  // model into hallucinating subjects from earlier conversations.
  const sid = makeSessionId();
  localStorage.setItem(SESSION_KEY, sid);
  touchSessionTimestamp();
  return sid;
}

// ---- Context --------------------------------------------------------------

const CeoChatContext = createContext<CeoChatState | null>(null);

export function CeoChatProvider({ children }: { children: React.ReactNode }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [sending, setSending] = useState(false);
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const mountedRef = useRef(true);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    mountedRef.current = true;
    setSessionId(getOrCreateSessionId());
    return () => { mountedRef.current = false; };
  }, []);

  // Load messages when session changes
  useEffect(() => {
    if (!sessionId) return;
    getAuthHeaders().then(headers => {
      fetch(`${API_URL}/api/ceo/chat/${sessionId}/history`, { headers })
        .then((r) => r.json())
        .then((d) => { if (mountedRef.current) setMessages(d.messages || []); })
        .catch(() => { if (mountedRef.current) setMessages([]); });
    });
  }, [sessionId]);

  const refreshSessions = useCallback(() => {
    const tid = getTenantId();
    if (!tid) return;
    getAuthHeaders().then(headers => {
      fetch(`${API_URL}/api/ceo/chat/sessions/${tid}`, { headers })
        .then((r) => r.json())
        .then((d) => { if (mountedRef.current) setSessions(d.sessions || []); })
        .catch(() => {});
    });
  }, []);

  useEffect(() => { refreshSessions(); }, [refreshSessions]);

  const cancel = useCallback(() => {
    if (!sending) return;
    abortRef.current?.abort();
    abortRef.current = null;
    if (mountedRef.current) {
      setSending(false);
      setMessages((p) => [...p, { role: "assistant", content: "Cancelled." }]);
    }
  }, [sending]);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || sending) return;

      setMessages((p) => [...p, { role: "user", content: trimmed }]);
      setSending(true);
      touchSessionTimestamp();

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const authHeaders = await getAuthHeaders();
        const res = await fetch(`${API_URL}/api/ceo/chat`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders },
          body: JSON.stringify({ session_id: sessionId, message: trimmed, tenant_id: getTenantId() }),
          signal: controller.signal,
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
      } catch (err: any) {
        if (mountedRef.current) {
          // AbortError means the user cancelled — already handled in cancel()
          if (err?.name !== "AbortError") {
            // Distinguish common failure modes so users get a useful message
            // instead of "is the backend running?" for every failure.
            const msg = err?.message || "";
            let text = "Couldn't reach the CEO. Please try again.";
            if (/Failed to fetch|NetworkError|network/i.test(msg)) {
              text = "Network error — check your internet connection and try again.";
            } else if (/timeout|timed out/i.test(msg)) {
              text = "The CEO took too long to respond. Try a simpler prompt or try again in a moment.";
            }
            setMessages((p) => [...p, { role: "assistant", content: text }]);
          }
        }
      }

      if (mountedRef.current) setSending(false);
      abortRef.current = null;
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
    touchSessionTimestamp();
    setSessionId(sid);
  }, []);

  const startNewChat = useCallback(() => {
    const sid = makeSessionId();
    localStorage.setItem(SESSION_KEY, sid);
    touchSessionTimestamp();
    setSessionId(sid);
    setMessages([]);
  }, []);

  const deleteSession = useCallback(async (sid: string) => {
    const tid = getTenantId();
    if (!tid || !sid) return;

    // Snapshot for rollback on error — the list disappears instantly,
    // but if the API call fails we restore it so the user doesn't
    // think a session vanished when it's actually still in the DB.
    const prevSessions = sessions;
    setSessions((list) => list.filter((s) => s.id !== sid));

    const wasCurrent = sid === sessionId;
    if (wasCurrent) {
      // Start a fresh chat state right away — the user's mental model
      // is "delete this conversation" → they expect the view to clear,
      // not to be left looking at a about-to-be-404 session.
      const fresh = makeSessionId();
      localStorage.setItem(SESSION_KEY, fresh);
      touchSessionTimestamp();
      setSessionId(fresh);
      setMessages([]);
    }

    try {
      await ceoChatApi.deleteSession(tid, sid);
    } catch {
      // Restore list on failure; the current-session reset is left as
      // a new empty chat (harmless — user can re-switch or discard).
      if (mountedRef.current) setSessions(prevSessions);
    }
  }, [sessions, sessionId]);

  const deleteSessions = useCallback(async (ids: string[]): Promise<number> => {
    const tid = getTenantId();
    const cleanIds = (ids || []).filter(Boolean);
    if (!tid || cleanIds.length === 0) return 0;

    const prevSessions = sessions;
    setSessions((list) => list.filter((s) => !cleanIds.includes(s.id)));

    const currentGotDeleted = cleanIds.includes(sessionId);
    if (currentGotDeleted) {
      const fresh = makeSessionId();
      localStorage.setItem(SESSION_KEY, fresh);
      touchSessionTimestamp();
      setSessionId(fresh);
      setMessages([]);
    }

    try {
      const res = await ceoChatApi.deleteSessions(tid, cleanIds);
      return res?.deleted ?? cleanIds.length;
    } catch {
      if (mountedRef.current) setSessions(prevSessions);
      return 0;
    }
  }, [sessions, sessionId]);

  const value: CeoChatState = { messages, sessions, sessionId, sending, pendingConfirmation, send, cancel, confirmAction, cancelAction, switchSession, startNewChat, refreshSessions, deleteSession, deleteSessions };

  return React.createElement(CeoChatContext.Provider, { value }, children);
}

// ---- Consumer hook --------------------------------------------------------

export function useCeoChat(): CeoChatState {
  const ctx = useContext(CeoChatContext);
  if (!ctx) throw new Error("useCeoChat must be used within <CeoChatProvider>");
  return ctx;
}
