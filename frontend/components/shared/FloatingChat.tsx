"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import { AGENT_COLORS, AGENT_NAMES } from "@/lib/agent-config";
import { useDraggable } from "@/lib/use-draggable";
import { useCeoChat } from "@/lib/use-ceo-chat";
import { formatDateAgo } from "@/lib/utils";
import { renderMarkdown } from "@/lib/render-markdown";
import { useSpeechToText, useTTS, sttErrorMessage } from "@/lib/use-voice";
import { useResizablePanel, type ResizeCorner } from "@/lib/use-resizable-panel";
import { useConfirm } from "@/lib/use-confirm";
import { useKeyboardState } from "@/lib/use-keyboard";
import ConfirmationDialog from "@/components/shared/ConfirmationDialog";

export default function FloatingChat() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  // panelRef kept for any future outside-click / focus logic. Not currently
  // wired to the panel div — resize is handled by the corner grip instead.
  const panelRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const seenCount = useRef(0); // tracks how many messages the user has seen

  const { messages, sessions, sessionId, sending, pendingConfirmation, send, cancel, confirmAction, cancelAction, switchSession, startNewChat, deleteSession, deleteSessions } = useCeoChat();
  const { confirm } = useConfirm();

  // Bulk-select state for the history dropdown. Cleared whenever the
  // dropdown closes so stale selections can't survive between opens.
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [bulkDeleting, setBulkDeleting] = useState(false);
  // Selection mode is OFF by default so the dropdown stays clean —
  // checkboxes appear only after clicking "Delete" to enter multi-
  // select mode. Exits on dropdown close and after a successful delete.
  const [selectMode, setSelectMode] = useState(false);
  useEffect(() => {
    if (!showHistory) {
      setSelectedIds(new Set());
      setSelectMode(false);
    }
  }, [showHistory]);

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

  const allSelected = sessions.length > 0 && selectedIds.size === sessions.length;
  const someSelected = selectedIds.size > 0 && selectedIds.size < sessions.length;

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
      setSelectMode(false);
    } finally {
      setBulkDeleting(false);
    }
  }
  const sendRef = useRef(send);
  sendRef.current = send;
  const stt = useSpeechToText(useCallback((text: string) => { if (text.trim()) sendRef.current(text.trim()); }, []));
  const tts = useTTS();
  const prevMsgCount = useRef(0);

  // Auto-read new assistant messages aloud
  useEffect(() => {
    if (messages.length > prevMsgCount.current && open) {
      const last = messages[messages.length - 1];
      if (last?.role === "assistant" && tts.enabled) tts.speak(last.content);
    }
    prevMsgCount.current = messages.length;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages.length, open]);

  const { pos, btnRef, handleMouseDown, handleTouchStart, handleClick } = useDraggable(
    typeof window !== "undefined" ? window.innerWidth - 200 : 1000,
    typeof window !== "undefined" ? window.innerHeight - 140 : 660,
    "ceo-chat",
  );

  // Hide the FAB when the on-screen keyboard is open AND the chat
  // panel itself isn't open. Keeps the bubble from blocking form
  // inputs (Sign Out / Change Password / etc) on other pages while
  // the user is typing. When the chat panel IS open, leaving the FAB
  // visible is fine because the panel takes the whole bottom area.
  const keyboard = useKeyboardState();

  // Mark all messages as seen when panel is open
  useEffect(() => {
    if (open) seenCount.current = messages.length;
  }, [open, messages]);

  // Global keyboard shortcut: Cmd+K (Mac) or Ctrl+K (Win/Linux) toggles
  // the chat panel from anywhere in the dashboard. Power-user feature
  // -- the target user (technical founder) expects this on day 2.
  // Skipped when typing in inputs/textareas/contenteditable so it
  // doesn't fight with the user's text input.
  useEffect(() => {
    function isTypingTarget(e: KeyboardEvent): boolean {
      const t = e.target as HTMLElement | null;
      if (!t) return false;
      const tag = t.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (t.isContentEditable) return true;
      return false;
    }
    function onKey(e: KeyboardEvent) {
      // Cmd+K / Ctrl+K toggles the panel (Mac uses metaKey, Win/Linux ctrlKey)
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((v) => !v);
        return;
      }
      // Plain "/" opens the panel and focuses input (when not typing)
      if (e.key === "/" && !isTypingTarget(e) && !e.metaKey && !e.ctrlKey && !e.altKey) {
        e.preventDefault();
        setOpen(true);
      }
      // Escape closes if open
      if (e.key === "Escape" && open) {
        setOpen(false);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  // Scroll to bottom on new messages AND when panel opens
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, open]);

  // Auto-grow textarea while voice input is live. The onChange auto-resize
  // only fires on user typing — when STT streams transcript via React props,
  // onChange never runs and the field stays at 1 row even for long dictations.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, [stt.transcript, stt.listening, input]);

  // Persistent panel — the panel stays open until the user explicitly
  // closes it (via the X button, the CEO Chat toggle, or Escape).
  // Clicking outside the panel or navigating to other pages doesn't
  // dismiss it. This was the old behaviour users complained about:
  // losing the in-progress message because they clicked the sidebar.

  function handleSend() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    send(text);
  }

  function handleSwitchSession(sid: string) {
    switchSession(sid);
    setShowHistory(false);
  }

  function handleNewChat() {
    startNewChat();
    setShowHistory(false);
  }

  // Panel position + size.
  //
  // Position is derived purely from the button: no independent drag, so the
  // two always read as ONE unit. Drag the button → the panel moves with it.
  //
  // Size is resizable via a visible corner grip (useResizablePanel hook)
  // and persisted to localStorage. Restored on mount. Default 420 × 520.
  const wH = typeof window !== "undefined" ? window.innerHeight : 800;
  const wW = typeof window !== "undefined" ? window.innerWidth : 1200;
  const PANEL_GAP = 8;
  const BUTTON_H = 52;

  // Resize handle goes on the panel corner FARTHEST from the button so
  // dragging it feels natural (drag away from button = grow).
  const isButtonRight = pos.x > wW * 0.4;
  const isButtonBottom = pos.y > wH * 0.35;
  const corner: ResizeCorner =
    isButtonRight
      ? (isButtonBottom ? "nw" : "sw")
      : (isButtonBottom ? "ne" : "se");

  const defaultH = Math.min(520, wH - 80);

  // Shared between useResizablePanel's direct-DOM path (during drag) and
  // React's render-time style (at rest). Same math for both so the panel
  // doesn't jump on mouseup.
  const computePanelPosition = useCallback(
    (s: { w: number; h: number }) => {
      const buttonRightEdge = pos.x + 170;
      const rawPanelX = isButtonRight ? buttonRightEdge - s.w : pos.x;
      const left = Math.min(Math.max(20, rawPanelX), wW - s.w - 20);
      const top = isButtonBottom
        ? Math.max(20, pos.y - s.h - PANEL_GAP)
        : Math.min(wH - s.h - 20, pos.y + BUTTON_H + PANEL_GAP);
      return { left, top };
    },
    [pos.x, pos.y, isButtonRight, isButtonBottom, wW, wH],
  );

  const { size: panelSize, startResize, cursorClass, handles } = useResizablePanel(
    "aria-ceo-chat-panel-size",
    { w: 420, h: defaultH },
    corner,
    { minW: 320, minH: 360 },
    { panelRef, computePosition: computePanelPosition },
  );

  const { left: basePanelX, top: basePanelY } = computePanelPosition(panelSize);

  const panelStyle: React.CSSProperties = {
    position: "fixed",
    width: panelSize.w,
    height: panelSize.h,
    left: basePanelX,
    top: basePanelY,
    zIndex: 61,
  };

  // Tailwind class for the corner position + matching border-radius of the
  // resize grip. Listed explicitly because Tailwind's JIT needs literal
  // class names — dynamic string interpolation isn't scanned.
  const cornerPos = {
    nw: "top-0 left-0",
    ne: "top-0 right-0",
    sw: "bottom-0 left-0",
    se: "bottom-0 right-0",
  }[corner];
  const cornerRound = {
    nw: "rounded-tl-xl",
    ne: "rounded-tr-xl",
    sw: "rounded-bl-xl",
    se: "rounded-br-xl",
  }[corner];

  if (pos.x < 0) return null;

  // Hide the FAB while the on-screen keyboard is open AND the chat
  // panel itself isn't open. Two reasons: (a) the FAB sits right at the
  // bottom of the screen, exactly where the keyboard typically pops up,
  // so it ends up overlapping the keyboard's top row of keys; (b) when
  // the user is typing into a form on another page (Sign Out / Change
  // Password / Settings inputs), having the FAB float on top of those
  // controls is just noise. When the chat panel IS open we leave the
  // FAB visible because the panel itself takes over the bottom area.
  const hideForKeyboard = keyboard.open && !open;
  if (hideForKeyboard) {
    return pendingConfirmation ? (
      <ConfirmationDialog
        data={pendingConfirmation}
        onConfirm={confirmAction}
        onCancel={cancelAction}
        loading={sending}
      />
    ) : null;
  }

  return (
    <>
      <button
        ref={btnRef}
        data-floating-widget="ceo-chat"
        onMouseDown={handleMouseDown}
        onTouchStart={handleTouchStart}
        onClick={() => handleClick() && setOpen(v => !v)}
        className="fixed left-0 top-0 z-[60] flex items-center gap-2.5 h-[52px] px-3.5 sm:px-5 rounded-2xl text-sm font-extrabold tracking-wide select-none cursor-grab active:cursor-grabbing will-change-transform touch-none"
        style={{
          transform: `translate3d(${pos.x}px, ${pos.y}px, 0)`,
          background: "linear-gradient(135deg, #534AB7 0%, #7C3AED 100%)",
          color: "#fff",
          boxShadow: "0 8px 30px rgba(83,74,183,0.35), 0 2px 8px rgba(124,58,237,0.2)",
        }}
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2.2} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
        </svg>
        {/* Label hidden on phones — the icon alone is the affordance.
            Halves the FAB's footprint so it stops blocking inbox/CRM
            content. Restored at sm+ where there's room to spare. */}
        <span className="hidden sm:inline">CEO Chat</span>
        {messages.length > seenCount.current && (
          <span className="bg-white text-[#534AB7] text-[11px] font-black px-2 py-0.5 rounded-full">{messages.length - seenCount.current}</span>
        )}
      </button>

      {open && (
        <div ref={panelRef} data-floating-widget="ceo-chat" style={panelStyle} className="relative bg-white rounded-xl border border-[#E0DED8] shadow-2xl flex flex-col overflow-hidden">
          {/* Resize handles — far edges + far corner, relative to the
              button anchor. Edges are thin strips with a matching cursor;
              the corner keeps a visible SVG grip. Near edges are omitted
              because the panel's near side is locked to the button. */}
          {handles.map((h) => {
            if (h === "n") return <div key={h} onMouseDown={startResize("n")} className="absolute left-0 right-0 top-0 h-1.5 cursor-ns-resize hover:bg-[#534AB7]/10 z-[62]" />;
            if (h === "s") return <div key={h} onMouseDown={startResize("s")} className="absolute left-0 right-0 bottom-0 h-1.5 cursor-ns-resize hover:bg-[#534AB7]/10 z-[62]" />;
            if (h === "e") return <div key={h} onMouseDown={startResize("e")} className="absolute top-0 bottom-0 right-0 w-1.5 cursor-ew-resize hover:bg-[#534AB7]/10 z-[62]" />;
            if (h === "w") return <div key={h} onMouseDown={startResize("w")} className="absolute top-0 bottom-0 left-0 w-1.5 cursor-ew-resize hover:bg-[#534AB7]/10 z-[62]" />;
            return (
              <div
                key={h}
                onMouseDown={startResize(h)}
                className={`absolute ${cornerPos} w-6 h-6 ${cursorClass} flex items-center justify-center hover:bg-[#534AB7]/10 ${cornerRound} transition-colors z-[63]`}
                title="Drag to resize"
              >
                <svg
                  className="w-3.5 h-3.5 text-[#534AB7]/60 pointer-events-none"
                  viewBox="0 0 16 16"
                  fill="none"
                  style={{
                    transform:
                      corner === "ne" ? "scaleX(-1)" :
                      corner === "sw" ? "scaleY(-1)" :
                      corner === "se" ? "rotate(180deg)" :
                      undefined,
                  }}
                >
                  <path d="M1 14 L14 1 M5 14 L14 5 M9 14 L14 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </div>
            );
          })}
          {/* Header — doubles as a drag handle. Grabbing it anywhere that
              isn't a button drags BOTH the panel and the toggle button
              together, because they share the same useDraggable position.
              Clicks on child buttons are filtered out so they still work. */}
          <div
            onMouseDown={(e) => {
              if ((e.target as HTMLElement).closest("button")) return;
              handleMouseDown(e);
            }}
            onTouchStart={(e) => {
              if ((e.target as HTMLElement).closest("button")) return;
              handleTouchStart(e);
            }}
            className="flex items-center gap-2 px-3 py-2.5 border-b border-[#E0DED8] shrink-0 cursor-grab active:cursor-grabbing select-none touch-none"
          >
            <button
              onClick={() => setShowHistory(v => !v)}
              className={`p-1.5 rounded-lg transition-colors ${showHistory ? "bg-[#EEEDFE] text-[#534AB7]" : "text-[#B0AFA8] hover:text-[#2C2C2A] hover:bg-[#F8F8F6]"}`}
              title="Chat history"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
            </button>
            <div className="w-6 h-6 rounded-md bg-[#534AB7] flex items-center justify-center">
              <span className="text-white text-[9px] font-bold">AI</span>
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-xs font-semibold text-[#2C2C2A]">ARIA CEO</h3>
            </div>
            <button onClick={handleNewChat} className="p-1.5 rounded-lg text-[#B0AFA8] hover:text-[#534AB7] hover:bg-[#EEEDFE] transition-colors" title="New chat">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
              </svg>
            </button>
            <span className="flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-[#1D9E75]" />
              <span className="text-[9px] text-[#1D9E75] font-medium">Online</span>
            </span>
            <button onClick={() => setOpen(false)} className="p-1 text-[#B0AFA8] hover:text-[#2C2C2A] transition-colors">
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
            </button>
          </div>

          {/* Body: session list OR chat messages */}
          {showHistory ? (
            <div className="flex-1 overflow-y-auto">
              {sessions.length === 0 ? (
                <p className="text-[10px] text-[#B0AFA8] text-center py-8">No previous chats</p>
              ) : (
                <>
                  {/* Toolbar — shows "Delete" toggle by default.
                      Clicking it enters selectMode which swaps the
                      toolbar for the master checkbox + bulk-delete
                      controls. Cancel exits without deleting. */}
                  {!selectMode ? (
                    <div className="flex items-center justify-end gap-1 px-3 py-1.5 border-b border-[#E0DED8]/40 bg-[#FAFAF8]">
                      <button
                        onClick={() => setSelectMode(true)}
                        className="flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium text-[#5F5E5A] hover:bg-white rounded border border-[#E0DED8]/70 transition"
                        aria-label="Enter selection mode to delete conversations"
                      >
                        <svg className="w-2.5 h-2.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                        </svg>
                        Delete
                      </button>
                    </div>
                  ) : (
                    <div className="flex items-center gap-2 px-3 py-1.5 border-b border-[#E0DED8]/40 bg-[#FAFAF8]">
                      <label className="flex items-center gap-1.5 cursor-pointer">
                        <input
                          type="checkbox"
                          aria-label="Select all conversations"
                          checked={allSelected}
                          ref={(el) => {
                            if (el) el.indeterminate = someSelected;
                          }}
                          onChange={toggleSelectAll}
                          className="w-3.5 h-3.5 accent-[#534AB7] cursor-pointer"
                        />
                        <span className="text-[10px] font-medium text-[#5F5E5A]">
                          {selectedIds.size > 0 ? `${selectedIds.size} selected` : "Select all"}
                        </span>
                      </label>
                      <div className="ml-auto flex items-center gap-1">
                        {selectedIds.size > 0 && (
                          <button
                            onClick={handleBulkDelete}
                            disabled={bulkDeleting}
                            className="flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium text-red-500 hover:bg-red-50 rounded transition disabled:opacity-60"
                          >
                            {bulkDeleting ? (
                              <>
                                <svg className="w-2.5 h-2.5 animate-spin" viewBox="0 0 24 24" fill="none">
                                  <circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="3" strokeDasharray="40 20" />
                                </svg>
                                Deleting...
                              </>
                            ) : (
                              <>Delete ({selectedIds.size})</>
                            )}
                          </button>
                        )}
                        <button
                          onClick={() => {
                            setSelectMode(false);
                            setSelectedIds(new Set());
                          }}
                          className="px-1.5 py-0.5 text-[10px] font-medium text-[#5F5E5A] hover:bg-white rounded border border-[#E0DED8]/70 transition"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                {sessions.map(s => {
                  const checked = selectedIds.has(s.id);
                  return (
                    // Flex row: checkbox | switch button | trash.
                    // Checkbox has its own hit area so it can't
                    // accidentally open the session.
                    <div
                      key={s.id}
                      className={`group flex items-stretch border-b border-[#E0DED8]/40 transition-colors ${
                        s.id === sessionId
                          ? "bg-[#EEEDFE] border-l-2 border-l-[#534AB7]"
                          : checked
                            ? "bg-[#EEEDFE]/50"
                            : "hover:bg-[#F8F8F6]"
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
                            className="w-3.5 h-3.5 accent-[#534AB7] cursor-pointer"
                          />
                        </label>
                      )}
                      <button
                        onClick={() => handleSwitchSession(s.id)}
                        className="flex-1 text-left px-2 py-2.5 min-w-0"
                      >
                        <p className={`text-[11px] font-medium truncate ${s.id === sessionId ? "text-[#534AB7]" : "text-[#2C2C2A]"}`}>
                          {s.title || "New chat"}
                        </p>
                        <p className="text-[9px] text-[#B0AFA8] mt-0.5">{formatDateAgo(s.updated_at)}</p>
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
                })}
                </>
              )}
            </div>
          ) : (
            <div className="flex-1 overflow-y-auto p-3 space-y-3">
              {messages.length === 0 && !sending && (
                <p className="text-xs text-[#B0AFA8] text-center py-8">Ask the CEO anything about your marketing.</p>
              )}
              {messages.map((m, i) => (
                <div key={i}>
                  <div className={`flex ${m.role === "user" ? "justify-end" : "justify-start"}`}>
                    <div className={`max-w-[85%] rounded-xl px-3 py-2 ${
                      m.role === "user"
                        ? "bg-[#534AB7] text-white rounded-br-sm"
                        : "bg-[#F8F8F6] text-[#2C2C2A] border border-[#E0DED8] rounded-bl-sm"
                    }`}>
                      <div className="text-xs leading-relaxed">{renderMarkdown(m.content)}</div>
                    </div>
                    {m.role === "assistant" && tts.supported && (
                      <button
                        onClick={() => tts.speaking ? tts.stop() : tts.speak(m.content)}
                        className="mt-1 p-1 rounded text-[#B0AFA8] hover:text-[#534AB7] transition-colors"
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
                  {m.delegations && m.delegations.length > 0 && (
                    <div className="mt-1.5 space-y-1">
                      {m.delegations.map((d, j) => (
                        <div key={j} className="flex items-center gap-2 px-2 py-1.5 rounded-lg border border-dashed text-[10px]" style={{ borderColor: AGENT_COLORS[d.agent] || "#E0DED8" }}>
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
                  <button
                    onClick={cancel}
                    className="ml-auto px-2 py-0.5 text-[11px] font-medium rounded border border-red-200 text-red-400 hover:bg-red-50 hover:text-red-600 transition-colors"
                  >
                    Cancel
                  </button>
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          )}

          {/* Input */}
          <div className="border-t border-[#E0DED8] px-3 py-2 shrink-0">
            {stt.error && (
              <div className="mb-1.5 flex items-center gap-1.5 rounded-md border border-amber-200 bg-amber-50 px-2 py-1 text-[10px] text-amber-800">
                <span className="flex-1 leading-tight">{sttErrorMessage(stt.error)}</span>
                <button onClick={stt.clearError} className="text-amber-700 hover:text-amber-900 font-medium shrink-0">×</button>
              </div>
            )}
            <div className="flex items-end gap-2">
              <textarea
                ref={textareaRef}
                value={stt.listening && stt.transcript ? stt.transcript : input}
                onChange={e => { if (!stt.listening) { setInput(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 200) + "px"; } }}
                onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
                placeholder={stt.listening ? "Listening... (sends after 3s of silence)" : "Ask the CEO..."} disabled={sending} rows={1}
                className="flex-1 min-h-[36px] max-h-[200px] px-3 py-2 bg-[#F8F8F6] border border-[#E0DED8] rounded-lg text-xs text-[#2C2C2A] placeholder:text-[#6B6A65] focus:outline-none focus:ring-1 focus:ring-[#534AB7]/30 disabled:opacity-50 resize-none" />
              {tts.supported && (
                <button
                  onClick={() => tts.setEnabled(!tts.enabled)}
                  className={`p-2 rounded-lg transition-colors ${tts.enabled ? "text-[#534AB7]" : "text-[#B0AFA8] hover:text-[#5F5E5A]"}`}
                  title={tts.enabled ? "Turn off auto-read" : "Turn on auto-read"}
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
                  className={`p-2 rounded-lg transition-colors ${
                    stt.listening
                      ? "bg-red-500 text-white animate-pulse"
                      : "text-[#B0AFA8] hover:text-[#534AB7] hover:bg-[#F8F8F6]"
                  }`}
                  title={stt.listening ? "Stop recording" : "Voice input"}
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 18.75a6 6 0 006-6v-1.5m-6 7.5a6 6 0 01-6-6v-1.5m6 7.5v3.75m-3.75 0h7.5M12 15.75a3 3 0 01-3-3V4.5a3 3 0 116 0v8.25a3 3 0 01-3 3z" />
                  </svg>
                </button>
              )}
              <button onClick={handleSend} disabled={!input.trim() || sending} className="p-2 bg-[#534AB7] text-white rounded-lg hover:bg-[#433AA0] transition-colors disabled:opacity-40">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      )}
      {/* CEO Action Confirmation Dialog */}
      {pendingConfirmation && (
        <ConfirmationDialog
          data={pendingConfirmation}
          onConfirm={confirmAction}
          onCancel={cancelAction}
          loading={sending}
        />
      )}
    </>
  );
}
