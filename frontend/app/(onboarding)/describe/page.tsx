"use client";

import React, { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

interface ChatMessage {
  role: "aria" | "user";
  text: string;
}

const TOPICS = [
  { key: "product_description", label: "Product description" },
  { key: "target_audience", label: "Target audience" },
  { key: "value_proposition", label: "Value proposition" },
  { key: "competitors", label: "Competitors" },
  { key: "marketing_goals", label: "Marketing goals" },
  { key: "budget_timeline", label: "Budget & timeline" },
  { key: "brand_voice", label: "Brand voice" },
  { key: "channels_platforms", label: "Channels & platforms" },
];

const TOPIC_LABELS: Record<string, string> = Object.fromEntries(TOPICS.map(t => [t.key, t.label]));

export default function DescribePage() {
  const router = useRouter();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [questionsAnswered, setQuestionsAnswered] = useState(0);
  const [isComplete, setIsComplete] = useState(false);
  const [skippedTopics, setSkippedTopics] = useState<string[]>([]);
  const [skipping, setSkipping] = useState(false);
  const [isRestart, setIsRestart] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    // Detect restart mode
    if (localStorage.getItem("aria_reonboarding_tenant_id")) {
      setIsRestart(true);
    }
    fetch(`${API_URL}/api/onboarding/start`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({}) })
      .then(r => r.json())
      .then(data => {
        setSessionId(data.session_id);
        if (data.message) {
          setMessages([{ role: "aria", text: data.message }]);
        }
      })
      .catch(() => {
        setMessages([{ role: "aria", text: "Hi! I'm ARIA, your AI marketing team. I need to ask you 8 quick questions to set up your marketing strategy. Let's start — what did you build?" }]);
      });
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function handleSend(e: React.FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || loading || !sessionId) return;

    setInput("");
    setMessages(prev => [...prev, { role: "user", text }]);
    setLoading(true);

    try {
      const res = await fetch(`${API_URL}/api/onboarding/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      const data = await res.json();
      setMessages(prev => [...prev, { role: "aria", text: data.message }]);
      if (data.questions_answered) setQuestionsAnswered(data.questions_answered);
      if (data.is_complete) setIsComplete(true);
    } catch {
      setMessages(prev => [...prev, { role: "aria", text: "Sorry, I had trouble processing that. Could you try again?" }]);
    }

    setLoading(false);
  }

  function handleContinue() {
    localStorage.setItem("aria_onboarding_session", sessionId);
    if (skippedTopics.length > 0) {
      localStorage.setItem("aria_skipped_topics", JSON.stringify(skippedTopics));
    }
    router.push("/review");
  }

  async function handleSkip() {
    if (!sessionId || skipping) return;
    setSkipping(true);
    try {
      const res = await fetch(`${API_URL}/api/onboarding/skip`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: sessionId }),
      });
      const data = await res.json();
      if (data.skipped_topic) {
        const label = TOPIC_LABELS[data.skipped_topic] || data.skipped_topic;
        setMessages(prev => [...prev, { role: "aria", text: `No worries, we'll skip "${label}" for now. You can fill it in later from your dashboard.` }]);
        setSkippedTopics(data.skipped_topics || []);
      }
      if (data.questions_answered) setQuestionsAnswered(data.questions_answered);
      if (data.is_complete) setIsComplete(true);
    } catch {
      // silently fail
    }
    setSkipping(false);
  }

  const totalQuestions = 8;
  const progress = Math.min((questionsAnswered / totalQuestions) * 100, 100);
  const currentQ = Math.min(questionsAnswered + 1, totalQuestions);

  return (
    <div className="flex flex-col h-[calc(100vh-73px)]">
      {/* ── Mobile sticky progress bar ── */}
      <div className="lg:hidden sticky top-0 z-10 bg-white border-b border-[#E0DED8] px-4 py-2.5 flex items-center gap-3">
        <span className="text-xs font-semibold text-[#534AB7] whitespace-nowrap">
          Q{currentQ} of {totalQuestions}
        </span>
        <div className="flex-1 h-1.5 bg-[#E0DED8] rounded-full">
          <div className="h-full bg-[#534AB7] rounded-full transition-all duration-500" style={{ width: `${progress}%` }} />
        </div>
        <div className="flex gap-1">
          {TOPICS.map((t, i) => {
            const isSkipped = skippedTopics.includes(t.key);
            const isAnswered = i < questionsAnswered && !isSkipped;
            const isActive = i === questionsAnswered && !isComplete;
            return (
              <div
                key={t.key}
                className={`w-2 h-2 rounded-full transition-colors ${
                  isAnswered ? "bg-[#1D9E75]"
                  : isSkipped ? "bg-[#BA7517]"
                  : isActive ? "bg-[#534AB7] ring-2 ring-[#534AB7]/30"
                  : "bg-[#E0DED8]"
                }`}
              />
            );
          })}
        </div>
      </div>

      {/* ── Main two-column layout ── */}
      <div className="flex flex-1 min-h-0 flex-col lg:flex-row">
        {/* ── Chat column ── */}
        <div className="flex-1 lg:w-[65%] flex flex-col min-h-0 border-r border-[#E0DED8]">
          {/* Restart banner */}
          {isRestart && (
            <div className="px-6 py-2.5 bg-[#FDEEE8] border-b border-[#D85A30]/20 flex items-center gap-2">
              <svg width="14" height="14" fill="none" viewBox="0 0 24 24"><path d="M1 4v6h6" stroke="#D85A30" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" stroke="#D85A30" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              <span className="text-xs font-medium text-[#D85A30]">Re-onboarding: your previous profile will be overwritten when you finish.</span>
            </div>
          )}

          {/* Chat header with question counter */}
          <div className="px-6 py-4 border-b border-[#E0DED8] flex items-center justify-between">
            <div>
              <h2 className="text-lg font-bold text-[#2C2C2A]">Tell ARIA about your product</h2>
              <p className="text-sm text-[#5F5E5A]">Answer questions so the CEO agent can build your GTM strategy</p>
            </div>
            <div className="hidden lg:flex items-center gap-2 px-3 py-1.5 bg-[#EEEDFE] rounded-full">
              <span className="text-xs font-semibold text-[#534AB7]">Question {currentQ} of {totalQuestions}</span>
            </div>
          </div>

          {/* Scrollable chat messages */}
          <div className="flex-1 overflow-y-auto px-6 py-6 space-y-4">
            {messages.map((msg, i) => (
              <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                {msg.role === "aria" && (
                  <div className="w-8 h-8 rounded-full bg-[#534AB7] flex items-center justify-center flex-shrink-0 mr-3 mt-0.5">
                    <span className="text-white text-xs font-bold">A</span>
                  </div>
                )}
                <div className={`max-w-[75%] rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                  msg.role === "user"
                    ? "bg-[#534AB7] text-white rounded-br-md"
                    : "bg-[#F8F8F6] text-[#2C2C2A] border border-[#E0DED8] rounded-bl-md"
                }`}>
                  {msg.text}
                </div>
              </div>
            ))}

            {loading && (
              <div className="flex justify-start">
                <div className="w-8 h-8 rounded-full bg-[#534AB7] flex items-center justify-center flex-shrink-0 mr-3">
                  <span className="text-white text-xs font-bold">A</span>
                </div>
                <div className="bg-[#F8F8F6] border border-[#E0DED8] rounded-2xl rounded-bl-md px-4 py-3">
                  <div className="flex gap-1">
                    <span className="w-2 h-2 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                    <span className="w-2 h-2 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                    <span className="w-2 h-2 bg-[#534AB7] rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                  </div>
                </div>
              </div>
            )}

            <div ref={bottomRef} />
          </div>

          {/* Input area */}
          <div className="px-6 py-4 border-t border-[#E0DED8]">
            <form onSubmit={handleSend} className="flex items-center gap-3">
              <input
                type="text"
                value={input}
                onChange={e => setInput(e.target.value)}
                placeholder="Type your answer..."
                disabled={loading}
                className="flex-1 h-11 rounded-lg border border-[#E0DED8] px-4 text-sm text-[#2C2C2A] placeholder:text-[#B0AFA8] outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7] transition disabled:opacity-60"
              />
              <button
                type="button"
                onClick={handleSkip}
                disabled={loading || skipping || isComplete}
                className="h-11 px-4 rounded-lg border border-[#E0DED8] text-sm font-medium text-[#5F5E5A] hover:bg-[#F8F8F6] hover:text-[#2C2C2A] transition flex-shrink-0 disabled:opacity-40"
              >
                Skip
              </button>
              <button
                type="submit"
                disabled={!input.trim() || loading}
                className="h-11 px-5 rounded-lg bg-[#534AB7] text-white text-sm font-semibold hover:bg-[#4840A0] transition flex-shrink-0 disabled:opacity-40"
              >
                Send
              </button>
            </form>
          </div>
        </div>

        {/* ── Sticky progress panel (desktop) ── */}
        <div className="hidden lg:block lg:w-[35%] bg-[#F8F8F6] overflow-y-auto">
          <div className="sticky top-0 p-6 space-y-4">
            <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
              <div className="flex items-center justify-between mb-5">
                <h3 className="text-base font-bold text-[#2C2C2A]">Onboarding Progress</h3>
                <span className="text-xs text-[#5F5E5A] bg-[#F8F8F6] px-2.5 py-1 rounded-full">
                  {questionsAnswered} of {totalQuestions}
                </span>
              </div>

              <div className="w-full h-1.5 bg-[#E0DED8] rounded-full mb-6">
                <div className="h-full bg-[#534AB7] rounded-full transition-all duration-500" style={{ width: `${progress}%` }} />
              </div>

              <div className="space-y-2">
                {TOPICS.map((topic, i) => {
                  const isSkipped = skippedTopics.includes(topic.key);
                  const isAnswered = i < questionsAnswered && !isSkipped;
                  const isActive = i === questionsAnswered && !isComplete;
                  return (
                    <div
                      key={topic.key}
                      className={`flex items-center gap-3 px-3 py-2.5 rounded-lg transition-colors ${
                        isActive ? "bg-[#EEEDFE] ring-1 ring-[#534AB7]/20" : ""
                      }`}
                    >
                      {isAnswered ? (
                        <div className="w-5 h-5 rounded-full bg-[#E6F7F0] flex items-center justify-center flex-shrink-0">
                          <svg width="12" height="12" fill="none" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" stroke="#1D9E75" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
                        </div>
                      ) : isSkipped ? (
                        <div className="w-5 h-5 rounded-full bg-[#FDF3E7] flex items-center justify-center flex-shrink-0">
                          <svg width="12" height="12" fill="none" viewBox="0 0 24 24"><path d="M5 12h14" stroke="#BA7517" strokeWidth="2.5" strokeLinecap="round"/></svg>
                        </div>
                      ) : isActive ? (
                        <div className="w-5 h-5 rounded-full bg-[#534AB7] flex items-center justify-center flex-shrink-0">
                          <div className="w-2 h-2 rounded-full bg-white" />
                        </div>
                      ) : (
                        <div className="w-5 h-5 rounded-full border-2 border-[#E0DED8] flex-shrink-0" />
                      )}
                      <span className={`text-sm ${
                        isActive ? "text-[#534AB7] font-semibold"
                        : isAnswered ? "text-[#2C2C2A] font-medium"
                        : isSkipped ? "text-[#BA7517]"
                        : "text-[#B0AFA8]"
                      }`}>
                        {topic.label}{isSkipped ? " (skipped)" : ""}
                      </span>
                      {isActive && (
                        <span className="ml-auto text-[10px] font-medium text-[#534AB7] bg-[#534AB7]/10 px-2 py-0.5 rounded-full">Current</span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>

            {isComplete && (
              <div className="p-4 bg-[#E6F7F0] rounded-xl border border-[#1D9E75]/20">
                <p className="text-sm font-semibold text-[#1D9E75] mb-2">Onboarding complete!</p>
                <p className="text-xs text-[#5F5E5A] mb-3">ARIA has enough information to build your GTM playbook.</p>
              </div>
            )}

            <button
              onClick={handleContinue}
              disabled={questionsAnswered < 3}
              className="w-full flex items-center justify-center gap-2 h-11 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#4840A0] transition shadow-sm disabled:opacity-40 disabled:cursor-not-allowed"
            >
              {isComplete ? "Review & finish" : "Continue to review"}
              <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </button>
            {questionsAnswered < 3 && (
              <p className="text-[10px] text-[#B0AFA8] text-center">Answer at least 3 questions to continue</p>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
