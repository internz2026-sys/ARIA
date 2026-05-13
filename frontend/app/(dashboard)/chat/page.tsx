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
        {/* Header — matches the FloatingChat widget's compact layout:
            history toggle | tiny AI badge | "ARIA CEO" | new chat | tiny
            online dot. The previous version (large star avatar + long
            subtitle + pulsing dot) felt heavyweight next to the widget,
            so this page now mirrors the widget aesthetic. Divider line
            removed at the user's request so the header bleeds into the
            chat area. */}
        <div className="flex items-center gap-2 px-3 py-2.5">
          <button
            onClick={() => setSidebarOpen(!sidebarOpen)}
            className={`p-1.5 rounded-lg transition-colors shrink-0 ${
              sidebarOpen ? "bg-[#EEEDFE] text-[#534AB7]" : "text-[#B0AFA8] hover:text-[#2C2C2A] hover:bg-[#F8F8F6]"
            }`}
            title={sidebarOpen ? "Hide history" : "Show history"}
            aria-label="Toggle chat history"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
          </button>
          <div className="w-6 h-6 rounded-md bg-[#534AB7] flex items-center justify-center shrink-0">
            <span className="text-white text-[9px] font-bold">AI</span>
          </div>
          <div className="flex-1 min-w-0">
            <h1 className="text-xs font-semibold text-[#2C2C2A] truncate">ARIA CEO</h1>
          </div>
          <button
            onClick={startNewChat}
            className="p-1.5 rounded-lg text-[#B0AFA8] hover:text-[#534AB7] hover:bg-[#EEEDFE] transition-colors shrink-0"
            title="New chat"
            aria-label="Start new chat"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
          </button>
          <span className="flex items-center gap-1 shrink-0">
            <span className="w-1.5 h-1.5 rounded-full bg-[#1D9E75]" />
            <span className="text-[9px] text-[#1D9E75] font-medium">Online</span>
          </span>
        </div>

        {/* Messages — bubble styling mirrors the FloatingChat widget:
            text-xs leading-relaxed, rounded-xl with one sharp tail
            corner, max-w-[85%], no avatar, no name label above each
            bubble. The full-page chat still gets a slightly wider
            content column on sm+ (max-w-2xl) so longer replies are
            readable; the bubble itself stays widget-sized so the
            visual identity matches everywhere. */}
        <div className="flex-1 overflow-y-auto px-3 py-3 sm:px-4 sm:py-4 min-h-0">
          {/* When there are no messages yet, the inner wrapper becomes
              a full-height flex column so the empty state pins to the
              vertical center of the chat area instead of clinging to
              the top with a giant void underneath. The moment a
              message lands, we drop back to the normal stacked layout
              (space-y) so replies start flowing from the top. */}
          <div
            className={`max-w-2xl mx-auto ${
              messages.length === 0 && !sending
                ? "h-full flex flex-col items-center justify-center"
                : "space-y-3"
            }`}
          >
          {messages.length === 0 && (
            <div className="flex flex-col items-center text-center px-3">
              <p className="text-xs text-[#B0AFA8] mb-5">Ask the CEO anything about your marketing.</p>
              {/* Suggestion chips — page-only enhancement over the
                  widget (which has no chips). Restyled to match the
                  widget's compact, low-chrome aesthetic: pill border,
                  subtle text, no card-y shadows. */}
              <div className="grid grid-cols-2 gap-1.5 sm:gap-2 w-full max-w-md">
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
                    className="text-left text-[11px] sm:text-xs px-2.5 py-2 leading-snug rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:border-[#534AB7] hover:text-[#534AB7] hover:bg-[#EEEDFE]/50 active:bg-[#EEEDFE]/50 transition-all"
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map((msg, i) => (
            <div key={i}>
              <div className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                <div
                  className={`max-w-[85%] rounded-xl px-3 py-2 ${
                    msg.role === "user"
                      ? "bg-[#534AB7] text-white rounded-br-sm"
                      : "bg-[#F8F8F6] text-[#2C2C2A] border border-[#E0DED8] rounded-bl-sm"
                  }`}
                >
                  <div className="text-xs leading-relaxed">{renderMarkdown(msg.content)}</div>
                </div>
                {msg.role === "assistant" && tts.supported && (
                  <button
                    onClick={() => tts.speaking ? tts.stop() : tts.speak(msg.content)}
                    className="ml-1 self-end mb-0.5 p-1 rounded text-[#B0AFA8] hover:text-[#534AB7] transition-colors"
                    title={tts.speaking ? "Stop reading" : "Read aloud"}
                  >
                    {tts.speaking ? (
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 5.25v13.5m-7.5-13.5v13.5" />
                      </svg>
                    ) : (
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
                      </svg>
                    )}
                  </button>
                )}
              </div>
              {msg.delegations && msg.delegations.length > 0 && (
                <div className="mt-1.5 space-y-1">
                  {msg.delegations.map((d, j) => (
                    <div
                      key={j}
                      className="flex items-center gap-2 px-2 py-1.5 rounded-lg border border-dashed text-[10px]"
                      style={{ borderColor: AGENT_COLORS[d.agent] || "#E0DED8" }}
                    >
                      <span className="font-semibold px-1.5 py-0.5 rounded text-white" style={{ backgroundColor: AGENT_COLORS[d.agent] || "#5F5E5A" }}>
                        {AGENT_NAMES[d.agent] || d.agent}
                      </span>
                      <span className="text-[#5F5E5A] truncate flex-1">{d.task}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}

          {sending && (
            <div className="flex items-center gap-2 text-xs text-[#5F5E5A]">
              <div className="flex gap-1">
                <span className="w-1.5 h-1.5 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                <span className="w-1.5 h-1.5 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                <span className="w-1.5 h-1.5 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
              </div>
              Thinking...
            </div>
          )}

          <div ref={bottomRef} />
          </div>
        </div>

        {/* Input — matches the FloatingChat widget's compact layout:
            textarea bg-[#F8F8F6] (not white), text-xs, min-h-[36px], and
            chrome-less mode buttons (no border ring when "off"). Safe-
            area padding kept so the send button isn't hidden behind the
            iPhone home-indicator. */}
        <div
          className="px-3 py-2 shrink-0"
          style={{ paddingBottom: "max(0.5rem, env(safe-area-inset-bottom))" }}
        >
          <div className="max-w-2xl mx-auto">
            {stt.error && (
              <div className="mb-1.5 flex items-center gap-1.5 rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-[10px] text-amber-800">
                <span className="flex-1 leading-tight">{sttErrorMessage(stt.error)}</span>
                <button onClick={stt.clearError} className="text-amber-700 hover:text-amber-900 font-medium shrink-0">×</button>
              </div>
            )}
            {/* Tighter action-button cluster so the textarea owns more
                width on mobile. p-1.5 (24×24 hit target → small icon)
                + gap-1 between buttons, and a slightly wider gap from
                the textarea so the visual grouping reads as
                "input | actions". */}
            <div className="flex items-end gap-1.5">
              <textarea
                ref={textareaRef}
                value={stt.listening && stt.transcript ? stt.transcript : input}
                onChange={(e) => { if (!stt.listening) { setInput(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px"; } }}
                onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
                placeholder={stt.listening ? "Listening... (sends after 3s of silence)" : "Ask the CEO..."}
                disabled={sending}
                rows={1}
                className="flex-1 min-w-0 min-h-[40px] max-h-[200px] px-3 py-2.5 bg-[#F8F8F6] border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] placeholder:text-[#6B6A65] focus:outline-none focus:ring-1 focus:ring-[#534AB7]/30 disabled:opacity-50 resize-none"
              />
              {tts.supported && (
                <button
                  onClick={() => tts.setEnabled(!tts.enabled)}
                  className={`p-1.5 rounded-lg transition-colors shrink-0 ${tts.enabled ? "text-[#534AB7]" : "text-[#B0AFA8] hover:text-[#5F5E5A]"}`}
                  title={tts.enabled ? "Turn off auto-read" : "Turn on auto-read"}
                  aria-label={tts.enabled ? "Turn off auto-read" : "Turn on auto-read"}
                >
                  {tts.enabled ? (
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M19.114 5.636a9 9 0 010 12.728M16.463 8.288a5.25 5.25 0 010 7.424M6.75 8.25l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
                    </svg>
                  ) : (
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M17.25 9.75L19.5 12m0 0l2.25 2.25M19.5 12l2.25-2.25M19.5 12l-2.25 2.25m-10.5-6l4.72-4.72a.75.75 0 011.28.53v15.88a.75.75 0 01-1.28.53l-4.72-4.72H4.51c-.88 0-1.704-.507-1.938-1.354A9.01 9.01 0 012.25 12c0-.83.112-1.633.322-2.396C2.806 8.756 3.63 8.25 4.51 8.25H6.75z" />
                    </svg>
                  )}
                </button>
              )}
              {stt.supported && (
                <button
                  onClick={stt.toggle}
                  className={`p-1.5 rounded-lg transition-colors shrink-0 ${
                    stt.listening
                      ? "bg-red-500 text-white animate-pulse"
                      : "text-[#B0AFA8] hover:text-[#534AB7] hover:bg-[#F8F8F6]"
                  }`}
                  title={stt.listening ? "Stop recording" : "Voice input"}
                  aria-label={stt.listening ? "Stop recording" : "Voice input"}
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 006-6v-1.5m-6 7.5a6 6 0 01-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 01-3-3V4.5a3 3 0 116 0v8.25a3 3 0 01-3 3z" />
                  </svg>
                </button>
              )}
              <button
                onClick={handleSend}
                disabled={!input.trim() || sending}
                className="p-2 bg-[#534AB7] text-white rounded-lg hover:bg-[#433AA0] transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
                aria-label="Send message"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
