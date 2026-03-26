"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import { AGENT_COLORS, AGENT_NAMES } from "@/lib/agent-config";
import { useDraggable } from "@/lib/use-draggable";
import { useCeoChat } from "@/lib/use-ceo-chat";
import { formatDateAgo } from "@/lib/utils";

function renderMd(text: string) {
  const parts: React.ReactNode[] = [];
  text.split("\n").forEach((line, li) => {
    if (li > 0) parts.push(<br key={`b${li}`} />);
    const re = /(\*\*(.+?)\*\*|\*(.+?)\*|`(.+?)`)/g;
    let last = 0, m, pi = 0;
    const lp: React.ReactNode[] = [];
    while ((m = re.exec(line)) !== null) {
      if (m.index > last) lp.push(line.slice(last, m.index));
      if (m[2]) lp.push(<strong key={`${li}-${pi++}`}>{m[2]}</strong>);
      else if (m[3]) lp.push(<em key={`${li}-${pi++}`}>{m[3]}</em>);
      else if (m[4]) lp.push(<code key={`${li}-${pi++}`} className="px-0.5 bg-black/10 rounded text-[10px] font-mono">{m[4]}</code>);
      last = m.index + m[0].length;
    }
    if (last < line.length) lp.push(line.slice(last));
    parts.push(...lp);
  });
  return parts;
}

export default function FloatingChat() {
  const [open, setOpen] = useState(false);
  const [input, setInput] = useState("");
  const [showHistory, setShowHistory] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const seenCount = useRef(0); // tracks how many messages the user has seen

  const { messages, sessions, sessionId, sending, send, switchSession, startNewChat } = useCeoChat();

  const { pos, btnRef, handleMouseDown, handleClick } = useDraggable(
    typeof window !== "undefined" ? window.innerWidth - 200 : 1000,
    typeof window !== "undefined" ? window.innerHeight - 140 : 660,
  );

  // Mark all messages as seen when panel is open
  useEffect(() => {
    if (open) seenCount.current = messages.length;
  }, [open, messages]);

  // Scroll to bottom on new messages AND when panel opens
  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: "smooth" }); }, [messages, open]);

  // Close on click outside (but not when clicking other floating widgets)
  useEffect(() => {
    if (!open) return;
    function h(e: MouseEvent) {
      const t = e.target as HTMLElement;
      if (btnRef.current?.contains(t) || panelRef.current?.contains(t)) return;
      if (t.closest?.("[data-floating-widget]")) return; // ignore other widgets
      setOpen(false);
    }
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open, btnRef]);

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

  // Panel position — offset from button so it follows when button is dragged
  const wH = typeof window !== "undefined" ? window.innerHeight : 800;
  const wW = typeof window !== "undefined" ? window.innerWidth : 1200;
  const pH = Math.min(520, wH - 80);
  const [panelOffset, setPanelOffset] = useState<{ dx: number; dy: number }>({ dx: 0, dy: 0 });
  const panelDragRef = useRef<{ startX: number; startY: number; startDx: number; startDy: number } | null>(null);

  // Reset offset when panel reopens
  useEffect(() => { if (open) setPanelOffset({ dx: 0, dy: 0 }); }, [open]);

  const basePanelX = Math.max(20, pos.x > wW * 0.4 ? pos.x + 170 - 420 : pos.x);
  const basePanelY = pos.y > wH * 0.35 ? Math.max(20, pos.y - pH - 12) : pos.y + 56 + 12;

  const panelStyle: React.CSSProperties = {
    position: "fixed", width: 420, maxWidth: "calc(100vw - 40px)", height: pH,
    left: basePanelX + panelOffset.dx,
    top: basePanelY + panelOffset.dy,
    zIndex: 61,
  };

  const onPanelHeaderDown = useCallback((e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("button")) return;
    e.preventDefault();
    panelDragRef.current = { startX: e.clientX, startY: e.clientY, startDx: panelOffset.dx, startDy: panelOffset.dy };
    function onMove(ev: MouseEvent) {
      const d = panelDragRef.current!;
      setPanelOffset({ dx: d.startDx + ev.clientX - d.startX, dy: d.startDy + ev.clientY - d.startY });
    }
    function onUp() { panelDragRef.current = null; document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [panelOffset]);

  if (pos.x < 0) return null;

  return (
    <>
      <button
        ref={btnRef}
        data-floating-widget="ceo-chat"
        onMouseDown={handleMouseDown}
        onClick={() => handleClick() && setOpen(v => !v)}
        className="fixed left-0 top-0 z-[60] flex items-center gap-2.5 h-[52px] px-5 rounded-2xl text-sm font-extrabold tracking-wide select-none cursor-grab active:cursor-grabbing will-change-transform"
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
        CEO Chat
        {messages.length > seenCount.current && (
          <span className="bg-white text-[#534AB7] text-[11px] font-black px-2.5 py-0.5 rounded-full">{messages.length - seenCount.current}</span>
        )}
      </button>

      {open && (
        <div ref={panelRef} data-floating-widget="ceo-chat" style={panelStyle} className="bg-white rounded-xl border border-[#E0DED8] shadow-2xl flex flex-col overflow-hidden">
          {/* Header — drag to move panel */}
          <div className="flex items-center gap-2 px-3 py-2.5 border-b border-[#E0DED8] shrink-0 cursor-grab active:cursor-grabbing" onMouseDown={onPanelHeaderDown}>
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
                sessions.map(s => (
                  <button
                    key={s.id}
                    onClick={() => handleSwitchSession(s.id)}
                    className={`w-full text-left px-3 py-2.5 border-b border-[#E0DED8]/40 transition-colors ${
                      s.id === sessionId ? "bg-[#EEEDFE] border-l-2 border-l-[#534AB7]" : "hover:bg-[#F8F8F6]"
                    }`}
                  >
                    <p className={`text-[11px] font-medium truncate ${s.id === sessionId ? "text-[#534AB7]" : "text-[#2C2C2A]"}`}>
                      {s.title || "New chat"}
                    </p>
                    <p className="text-[9px] text-[#B0AFA8] mt-0.5">{formatDateAgo(s.updated_at)}</p>
                  </button>
                ))
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
                      <p className="text-xs leading-relaxed whitespace-pre-wrap">{renderMd(m.content)}</p>
                    </div>
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
                </div>
              )}
              <div ref={bottomRef} />
            </div>
          )}

          {/* Input */}
          <div className="border-t border-[#E0DED8] px-3 py-2 shrink-0">
            <div className="flex items-center gap-2">
              <input type="text" value={input} onChange={e => setInput(e.target.value)} onKeyDown={e => e.key === "Enter" && handleSend()}
                placeholder="Ask the CEO..." disabled={sending}
                className="flex-1 px-3 py-2 bg-[#F8F8F6] border border-[#E0DED8] rounded-lg text-xs text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-1 focus:ring-[#534AB7]/30 disabled:opacity-50" />
              <button onClick={handleSend} disabled={!input.trim() || sending} className="p-2 bg-[#534AB7] text-white rounded-lg hover:bg-[#433AA0] transition-colors disabled:opacity-40">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                </svg>
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
