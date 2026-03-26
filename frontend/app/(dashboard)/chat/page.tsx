"use client";

import React, { useState, useEffect, useRef } from "react";
import { supabase } from "@/lib/supabase";
import { AGENT_COLORS, AGENT_NAMES } from "@/lib/agent-config";
import { useCeoChat, type ChatMessage } from "@/lib/use-ceo-chat";
import { formatDateAgo } from "@/lib/utils";

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
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  const { messages, sessions, sessionId, sending, send, switchSession, startNewChat } = useCeoChat();

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

  function handleSend() {
    const text = input.trim();
    if (!text) return;
    setInput("");
    send(text);
  }

  return (
    <div className="flex h-[calc(100vh-120px)]">
      {/* ─── Chat History Sidebar ─── */}
      <div className={`${sidebarOpen ? "w-[260px]" : "w-0"} shrink-0 transition-all duration-200 overflow-hidden border-r border-[#E0DED8]`}>
        <div className="w-[260px] h-full flex flex-col bg-[#F8F8F6]">
          <div className="px-3 py-3 border-b border-[#E0DED8] flex items-center justify-between">
            <span className="text-xs font-semibold text-[#5F5E5A] uppercase tracking-wide">Chat History</span>
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

          <div className="flex-1 overflow-y-auto">
            {sessions.length === 0 ? (
              <p className="text-xs text-[#B0AFA8] text-center py-6">No chats yet</p>
            ) : (
              sessions.map(s => (
                <button
                  key={s.id}
                  onClick={() => switchSession(s.id)}
                  className={`w-full text-left px-3 py-2.5 border-b border-[#E0DED8]/50 transition group ${
                    s.id === sessionId
                      ? "bg-white border-l-2 border-l-[#534AB7]"
                      : "hover:bg-white/60"
                  }`}
                >
                  <p className={`text-xs font-medium truncate ${s.id === sessionId ? "text-[#534AB7]" : "text-[#2C2C2A]"}`}>
                    {s.title || "New chat"}
                  </p>
                  <p className="text-[10px] text-[#B0AFA8] mt-0.5">{formatDateAgo(s.updated_at)}</p>
                </button>
              ))
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
                  <div className="w-8 h-8 rounded-lg bg-[#534AB7] flex items-center justify-center shrink-0 mt-1">
                    <span className="text-white text-xs font-bold">AI</span>
                  </div>
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
                <div className="w-8 h-8 rounded-lg bg-[#534AB7] flex items-center justify-center shrink-0">
                  <span className="text-white text-xs font-bold">AI</span>
                </div>
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
        <div className="border-t border-[#E0DED8] px-4 pt-3 pb-2">
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && handleSend()}
              placeholder="Ask the CEO agent anything about your marketing..."
              disabled={sending}
              className="flex-1 px-4 py-3 bg-white border border-[#E0DED8] rounded-xl text-sm text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7] disabled:opacity-60"
            />
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
