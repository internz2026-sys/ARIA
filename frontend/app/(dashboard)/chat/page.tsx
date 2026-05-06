"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import { supabase } from "@/lib/supabase";
import { AGENT_COLORS, AGENT_NAMES } from "@/lib/agent-config";
import { useCeoChat, type ChatMessage } from "@/lib/use-ceo-chat";
import { formatDateAgo } from "@/lib/utils";
import { useSpeechToText, useTTS, sttErrorMessage } from "@/lib/use-voice";
import { useConfirm } from "@/lib/use-confirm";
import { useBelowBreakpoint } from "@/lib/use-breakpoint";

function renderMarkdown(text: string) {
  const parts: React.ReactNode[] = [];
  text.split("\n").forEach((line, lineIdx) => {
    if (lineIdx > 0) parts.push(<br key={`br-${lineIdx}`} />);
    const regex = /(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)/g;
    let lastIndex = 0;
    let match;
    const lineParts: React.ReactNode[] = [];
    let partIdx = 0;
    while ((match = regex.exec(line)) !== null) {
      if (match.index > lastIndex) lineParts.push(line.slice(lastIndex, match.index));
      if (match[2]) lineParts.push(<strong key={`${lineIdx}-${partIdx++}`}>{match[2]}</strong>);
      else if (match[3]) lineParts.push(<em key={`${lineIdx}-${partIdx++}`}>{match[3]}</em>);
      else if (match[4]) lineParts.push(
        <code key={`${lineIdx}-${partIdx++}`} className="px-1 py-0.5 bg-black/10 rounded text-xs font-mono">{match[4]}</code>
      );
      lastIndex = match.index + match[0].length;
    }
    if (lastIndex < line.length) lineParts.push(line.slice(lastIndex));
    parts.push(...lineParts);
  });
  return parts;
}

export default function CEOChatPage() {
  const [userName, setUserName] = useState("");
  // Default the sidebar OPEN; on mobile we override to closed once the
  // breakpoint hook reports below `lg`. The drawer renders as an overlay
  // on mobile, so starting closed keeps the chat readable instead of
  // forcing a 260px sidebar into a 390px viewport.
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const isMobile = useBelowBreakpoint("lg");

  useEffect(() => {
    if (isMobile) setSidebarOpen(false);
  }, [isMobile]);

  const { messages, sessions, sessionId, sending, send, switchSession, startNewChat, deleteSession, deleteSessions } = useCeoChat();
  const { confirm } = useConfirm();

  // ── Bulk select state ────────────────────────────────────────────
  // `selectedIds` is a Set so toggling is O(1) and the master-checkbox
  // "select all / some / none" state is a cheap size comparison.
  // Cleared automatically when the session list changes (eg. after a
  // bulk delete), when we unmount, and whenever the sidebar closes.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);
  // Selection mode is OFF by default so the sidebar stays clean —
  // checkboxes only appear after the user clicks "Delete" to enter
  // multi-select mode. Cancel exits without deleting.
  const [selectMode, setSelectMode] = useState(false);

  // Clear selection when the user navigates away (unmount) — prevents
  // accidental deletions on return to the page with stale selections.
  useEffect(() => {
    return () => setSelectedIds(new Set());
  }, []);

  // Clear selection when the sidebar is hidden (e.g. user collapses it
  // to focus on a conversation) so re-opening is a clean slate.
  useEffect(() => {
    if (!sidebarOpen) setSelectedIds(new Set());
  }, [sidebarOpen]);

  const toggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleSelectAll = useCallback(() => {
    setSelectedIds((prev) =>
      prev.size === sessions.length ? new Set() : new Set(sessions.map((s) => s.id)),
    );
  }, [sessions]);

  async function handleDeleteSession(sid: string, title: string) {
    const ok = await confirm({
      title: "Delete this conversation?",
      message: `"${title || "New chat"}" and all its messages will be permanently removed.`,
      confirmLabel: "Delete",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (ok) await deleteSession(sid);
  }

  async function handleBulkDelete() {
    const ids = Array.from(selectedIds);
    if (ids.length === 0 || bulkDeleting) return;
    const ok = await confirm({
      title: `Delete ${ids.length} conversation${ids.length === 1 ? "" : "s"}?`,
      message: "This cannot be undone. All messages in these conversations will be permanently removed.",
      confirmLabel: `Delete ${ids.length}`,
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    setBulkDeleting(true);
    try {
      await deleteSessions(ids);
      setSelectedIds(new Set());
      // Exit selection mode on success — the expected mental model
      // is "I deleted what I wanted, now get me back to the normal
      // sidebar."
      setSelectMode(false);
    } finally {
      setBulkDeleting(false);
    }
  }

  const allSelected = sessions.length > 0 && selectedIds.size === sessions.length;
  const someSelected = selectedIds.size > 0 && selectedIds.size < sessions.length;
  const sendRef = useRef(send);
  sendRef.current = send;
  const stt = useSpeechToText(useCallback((text: string) => { if (text.trim()) sendRef.current(text.trim()); }, []));
  const tts = useTTS();
  const prevMsgCount = useRef(0);

  // Auto-read new assistant messages aloud
  useEffect(() => {
    if (messages.length > prevMsgCount.current) {
      const last = messages[messages.length - 1];
      if (last?.role === "assistant" && tts.enabled) tts.speak(last.content);
    }
    prevMsgCount.current = messages.length;
  }, [messages, tts]);

  // Load user name from Supabase
  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (session?.user) {
        const meta = session.user.user_metadata;
        setUserName(meta?.full_name || meta?.name || session.user.email?.split("@")[0] || "User");
      }
    });
  }, []);

  // Scroll to bottom on new messages
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Auto-grow the textarea while voice input is live. onChange-based resize
  // doesn't fire when STT streams transcript via React props.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 260) + "px";
  }, [stt.transcript, stt.listening, input]);

  function handleSend() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    send(text);
  }

  return (
    // `@container/chat` lets the inner sidebar/main split decide its
    // layout from the actual chat-area width rather than the viewport,
    // which is what we want once the dashboard sidebar may collapse.
    // `dvh` (dynamic viewport height) shrinks when the mobile keyboard
    // opens, so the chat input stays visible above the keyboard instead
    // of being pushed off-screen.
    <div className="@container/chat flex h-[calc(100dvh-120px)] relative">
      {/* Mobile drawer backdrop. Renders only below `lg` and only when
          the sidebar is open — desktop never sees it. Tap to dismiss. */}
      {isMobile && sidebarOpen && (
        <div
          className="fixed inset-0 bg-black/40 z-40"
          onClick={() => setSidebarOpen(false)}
          aria-hidden="true"
        />
      )}

      {/* ─── Chat History Sidebar ───
          Mobile (<lg): absolute-positioned overlay drawer, slides in
          from the left with a backdrop above. Doesn't push the chat
          column, so the chat keeps the full container width.
          Desktop (lg+): inline column that animates from 0 → 260px and
          pushes the chat column. */}
      <div
        className={`shrink-0 transition-all duration-200 overflow-hidden border-r border-[#E0DED8] bg-[#F8F8F6]
          ${isMobile
            ? `absolute inset-y-0 left-0 z-50 shadow-2xl ${sidebarOpen ? "w-[280px]" : "w-0"}`
            : `relative ${sidebarOpen ? "w-[260px]" : "w-0"}`
          }`}
      >
        <div className={`${isMobile ? "w-[280px]" : "w-[260px]"} h-full flex flex-col bg-[#F8F8F6]`}>
          <div className="px-3 py-3 border-b border-[#E0DED8] flex items-center justify-between gap-2">
            <span className="text-xs font-semibold text-[#5F5E5A] uppercase tracking-wide">Chat History</span>
            <div className="flex items-center gap-1.5">
              {sessions.length > 0 && (
                <button
                  onClick={() => {
                    // Toggle selection mode. Exiting always clears
                    // pending selections so re-entering is a clean
                    // slate — matches "Cancel" semantics.
                    setSelectMode((m) => !m);
                    setSelectedIds(new Set());
                  }}
                  className={`flex items-center gap-1 px-2 py-1 text-[10px] font-medium rounded-md transition ${
                    selectMode
                      ? "bg-[#EEEDFE] text-[#534AB7] border border-[#534AB7]/30"
                      : "border border-[#E0DED8] text-[#5F5E5A] hover:bg-white"
                  }`}
                  aria-label={selectMode ? "Exit selection mode" : "Enter selection mode to delete conversations"}
                >
                  {selectMode ? (
                    <>
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                      </svg>
                      Cancel
                    </>
                  ) : (
                    <>
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                      </svg>
                      Delete
                    </>
                  )}
                </button>
              )}
              <button
                onClick={startNewChat}
                className="flex items-center gap-1 px-2 py-1 text-[10px] font-medium bg-[#534AB7] text-white rounded-md hover:bg-[#433AA0] transition"
              >
                <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
                </svg>
                New
              </button>
            </div>
          </div>

          {/* Master checkbox + bulk action bar. Only rendered in
              selection mode so the default sidebar stays clean. */}
          {sessions.length > 0 && selectMode && (
            <div className="flex items-center gap-2 px-3 py-2 border-b border-[#E0DED8] bg-white/50">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  aria-label="Select all conversations"
                  checked={allSelected}
                  ref={(el) => {
                    if (el) el.indeterminate = someSelected;
                  }}
                  onChange={toggleSelectAll}
                  className="w-4 h-4 accent-[#534AB7] cursor-pointer"
                />
                <span className="text-[10px] font-medium text-[#5F5E5A]">
                  {selectedIds.size > 0 ? `${selectedIds.size} selected` : "Select all"}
                </span>
              </label>
              {selectedIds.size > 0 && (
                <button
                  onClick={handleBulkDelete}
                  disabled={bulkDeleting}
                  className="ml-auto flex items-center gap-1 px-2 py-1 text-[10px] font-medium text-red-500 hover:bg-red-50 rounded-md transition disabled:opacity-60"
                >
                  {bulkDeleting ? (
                    <>
                      <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none">
                        <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="40 20" />
                      </svg>
                      Deleting...
                    </>
                  ) : (
                    <>
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                      </svg>
                      Delete ({selectedIds.size})
                    </>
                  )}
                </button>
              )}
            </div>
          )}

          <div className="flex-1 overflow-y-auto">
            {sessions.length === 0 ? (
              <p className="text-xs text-[#B0AFA8] text-center py-6">No chats yet</p>
            ) : (
              sessions.map(s => {
                const checked = selectedIds.has(s.id);
                return (
                  // Flex row: checkbox | switch button (flex-1) | trash.
                  // The checkbox is a distinct hit area — clicking it
                  // toggles selection and never opens the session.
                  <div
                    key={s.id}
                    className={`group flex items-stretch border-b border-[#E0DED8]/50 transition ${
                      s.id === sessionId
                        ? "bg-white border-l-2 border-l-[#534AB7]"
                        : checked
                          ? "bg-[#EEEDFE]/50"
                          : "hover:bg-white/60"
                    }`}
                  >
                    {selectMode && (
                      <label
                        className="flex items-center pl-3 pr-1 cursor-pointer"
                        onClick={(e) => e.stopPropagation()}
                      >
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => toggleSelect(s.id)}
                          aria-label={`Select conversation ${s.title || "New chat"}`}
                          className="w-4 h-4 accent-[#534AB7] cursor-pointer"
                        />
                      </label>
                    )}
                    <button
                      onClick={() => switchSession(s.id)}
                      className="flex-1 text-left px-2 py-2.5 min-w-0"
                    >
                      <p className={`text-xs font-medium truncate ${s.id === sessionId ? "text-[#534AB7]" : "text-[#2C2C2A]"}`}>
                        {s.title || "New chat"}
                      </p>
                      <p className="text-[10px] text-[#B0AFA8] mt-0.5">{formatDateAgo(s.updated_at)}</p>
                    </button>
                    <button
                      onClick={() => handleDeleteSession(s.id, s.title)}
                      className="px-2 text-[#B0AFA8] hover:text-red-500 opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity"
                      title="Delete this conversation"
                      aria-label="Delete conversation"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                      </svg>
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </div>
      </div>

      {/* ─── Main Chat Area ─── */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Header */}
        <div className="flex items-center gap-3 px-4 pb-3 border-b border-[#E0DED8]">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className="p-1.5 rounded-lg text-[#5F5E5A] hover:bg-[#F8F8F6] hover:text-[#2C2C2A] transition"
            title={sidebarOpen ? "Hide history" : "Show history"}
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
            </svg>
          </button>
          <div className="w-9 h-9 rounded-xl bg-[#534AB7] flex items-center justify-center shrink-0">
            <svg className="w-4.5 h-4.5 text-white" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" />
            </svg>
          </div>
          <div className="flex-1 min-w-0">
            <h1 className="text-base font-semibold text-[#2C2C2A]">Chat with CEO Agent</h1>
            <p className="text-[10px] text-[#5F5E5A] truncate">Your Chief Marketing Strategist — delegates tasks to Content Writer, Email Marketer, Social Manager, and Ad Strategist</p>
          </div>
          <div className="flex items-center gap-1.5">
            <span className="relative flex h-2 w-2">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-[#1D9E75] opacity-75" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-[#1D9E75]" />
            </span>
            <span className="text-xs font-medium text-[#1D9E75]">Online</span>
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto space-y-4 p-4 pb-2">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-center px-6">
              <div className="w-14 h-14 rounded-2xl bg-[#EEEDFE] flex items-center justify-center mb-4">
                <svg className="w-7 h-7 text-[#534AB7]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
                </svg>
              </div>
              <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">How can I help you today?</h3>
              <p className="text-sm text-[#5F5E5A] max-w-md mb-6">
                I&apos;m your Chief Marketing Strategist. Tell me what you need and I&apos;ll either handle it myself or delegate to the right agent.
              </p>
              <div className="grid grid-cols-2 gap-2 w-full max-w-md">
                {[
                  "Write a blog post about my product",
                  "Create a welcome email sequence",
                  "Plan this week's social media posts",
                  "Set up a Facebook ad campaign",
                  "Review my GTM strategy",
                  "What should I focus on this week?",
                ].map((suggestion) => (
                  <button
                    key={suggestion}
                    onClick={() => setInput(suggestion)}
                    className="text-left text-xs p-3 rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:border-[#534AB7] hover:text-[#534AB7] hover:bg-[#EEEDFE]/50 transition-all"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg, i) => (
            <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
              <div className={`max-w-[75%] ${msg.role === "user" ? "" : "flex gap-3"}`}>
                {msg.role === "assistant" && (
                  <img src="/logo.png" alt="ARIA" className="w-8 h-8 rounded-full object-cover shrink-0 mt-1" />
                )}
                <div>
                  <div
                    className={`rounded-2xl px-4 py-3 ${
                      msg.role === "user"
                        ? "bg-[#534AB7] text-white rounded-br-md"
                        : "bg-[#F8F8F6] text-[#2C2C2A] border border-[#E0DED8] rounded-bl-md"
                    }`}
                  >
                    <div className="flex items-center gap-2 mb-1">
                      <span className="text-[10px] font-semibold opacity-70">
                        {msg.role === "user" ? userName : "ARIA CEO"}
                      </span>
                    </div>
                    <p className="text-sm leading-relaxed whitespace-pre-wrap">{renderMarkdown(msg.content)}</p>
                  </div>
                  {msg.role === "assistant" && tts.supported && (
                    <button
                      onClick={() => tts.speaking ? tts.stop() : tts.speak(msg.content)}
                      className="mt-1 p-1 rounded text-[#B0AFA8] hover:text-[#534AB7] transition-colors"
                      title={tts.speaking ? "Stop reading" : "Read aloud"}
                    >
                      {tts.speaking ? (
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 5.25v13.5m-7.5-13.5v13.5" />
                        </svg>
                      ) : (
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
                        </svg>
                      )}
                    </button>
                  )}

                  {msg.delegations && msg.delegations.length > 0 && (
                    <div className="mt-2 space-y-2">
                      {msg.delegations.map((d, j) => (
                        <div
                          key={j}
                          className="flex items-start gap-2 p-2.5 rounded-lg border border-dashed"
                          style={{ borderColor: AGENT_COLORS[d.agent] || "#E0DED8" }}
                        >
                          <svg className="w-4 h-4 shrink-0 mt-0.5" fill="none" stroke={AGENT_COLORS[d.agent] || "#5F5E5A"} strokeWidth={2} viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 4.5L21 12m0 0l-7.5 7.5M21 12H3" />
                          </svg>
                          <div>
                            <div className="flex items-center gap-1.5">
                              <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded text-white" style={{ backgroundColor: AGENT_COLORS[d.agent] || "#5F5E5A" }}>
                                {AGENT_NAMES[d.agent] || d.agent}
                              </span>
                              <span className="text-[10px] text-[#5F5E5A]">{d.priority} priority</span>
                            </div>
                            <p className="text-xs text-[#5F5E5A] mt-1">{d.task}</p>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            </div>
          ))}

          {sending && (
            <div className="flex justify-start">
              <div className="flex gap-3">
                <img src="/logo.png" alt="ARIA" className="w-8 h-8 rounded-full object-cover shrink-0" />
                <div className="bg-[#F8F8F6] border border-[#E0DED8] rounded-2xl rounded-bl-md px-4 py-3">
                  <div className="flex items-center gap-2">
                    <div className="flex gap-1">
                      <span className="w-2 h-2 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                      <span className="w-2 h-2 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                      <span className="w-2 h-2 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                    </div>
                    <span className="text-xs text-[#5F5E5A]">CEO is thinking...</span>
                  </div>
                </div>
              </div>
            </div>
          )}

          <div ref={bottomRef} />
        </div>

        {/* Input */}
        {/* Input area — sticks to the bottom of the flex-col, and on
            mobile adds the iOS home-indicator inset so the send button
            isn't hidden behind the safe-area. `pb-[env(safe-area-inset-
            bottom)]` is additive to the explicit pb so the input stays
            readable on both iOS and Android. */}
        <div
          className="border-t border-[#E0DED8] px-4 pt-3 pb-2"
          style={{ paddingBottom: "max(0.5rem, env(safe-area-inset-bottom))" }}
        >
          {stt.error && (
            <div className="mb-2 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
              <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
              </svg>
              <span className="flex-1">{sttErrorMessage(stt.error)}</span>
              <button onClick={stt.clearError} className="text-amber-700 hover:text-amber-900 font-medium">
                Dismiss
              </button>
            </div>
          )}
          <div className="flex items-end gap-2">
            <textarea
              ref={textareaRef}
              value={stt.listening && stt.transcript ? stt.transcript : input}
              onChange={(e) => { if (!stt.listening) { setInput(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 260) + "px"; } }}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
              placeholder={stt.listening ? "Listening... (sends after 3s of silence)" : "Ask the CEO agent anything about your marketing..."}
              disabled={sending}
              rows={1}
              className="flex-1 min-h-[48px] max-h-[260px] px-4 py-3 bg-white border border-[#E0DED8] rounded-xl text-sm text-[#2C2C2A] placeholder:text-[#6B6A65] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7] disabled:opacity-60 resize-none"
            />
            {tts.supported && (
              <button
                onClick={() => tts.setEnabled(!tts.enabled)}
                className={`p-3 rounded-xl transition-colors border border-[#E0DED8] ${
                  tts.enabled ? "text-[#534AB7] bg-[#EEEDFE]" : "text-[#B0AFA8] hover:text-[#5F5E5A] hover:bg-[#F8F8F6]"
                }`}
                title={tts.enabled ? "Turn off auto-read" : "Turn on auto-read"}
              >
                {tts.enabled ? (
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
                  </svg>
                ) : (
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M17.25 9.75L19.5 12m0 0l2.25 2.25M19.5 12l2.25-2.25M19.5 12l-2.25 2.25m-10.5-6l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
                  </svg>
                )}
              </button>
            )}
            {stt.supported && (
              <button
                onClick={stt.toggle}
                className={`p-3 rounded-xl transition-colors ${
                  stt.listening
                    ? "bg-red-500 text-white animate-pulse"
                    : "border border-[#E0DED8] text-[#5F5E5A] hover:text-[#534AB7] hover:bg-[#F8F8F6]"
                }`}
                title={stt.listening ? "Stop recording" : "Voice input"}
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 006-6v-1.5m-6 7.5a6 6 0 01-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 01-3-3V4.5a3 3 0 116 0v8.25a3 3 0 01-3 3z" />
                </svg>
              </button>
            )}
            <button
              onClick={handleSend}
              disabled={!input.trim() || sending}
              className="p-3 bg-[#534AB7] text-white rounded-xl hover:bg-[#433AA0] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
              </svg>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
