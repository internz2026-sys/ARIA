"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import { useRouter } from "next/navigation";
import { renderMarkdown } from "@/lib/render-markdown";
import { useSpeechToText, useTTS, sttErrorMessage } from "@/lib/use-voice";
import { API_URL, authFetch } from "@/lib/api";

interface ChatMessage {
  role: "aria" | "user";
  text: string;
}

const TOPICS = [
  { key: "business_name", label: "Business name" },
  { key: "product_or_offer", label: "Product / offer" },
  { key: "target_audience", label: "Target audience" },
  { key: "problem_solved", label: "Problem solved" },
  { key: "differentiator", label: "Differentiator" },
  { key: "channels", label: "Channels" },
  { key: "brand_voice", label: "Brand voice" },
  { key: "goal_30_days", label: "30-day goal" },
];

const TOPIC_LABELS: Record<string, string> = Object.fromEntries(TOPICS.map(t => [t.key, t.label]));

/** Strip JSON blocks, code fences, and config keys from visible chat messages. */
function sanitizeChatMessage(text: string): string {
  let cleaned = text;
  // Remove fenced code blocks (```json ... ``` or ``` ... ```)
  cleaned = cleaned.replace(/```[\s\S]*?```/g, "");
  // Remove standalone JSON objects
  cleaned = cleaned.replace(/^\s*\{[\s\S]*?\}\s*$/gm, "");
  // Remove config section headers
  cleaned = cleaned.replace(/\*{0,2}Extracted Config:?\*{0,2}\s*/g, "");
  cleaned = cleaned.replace(/\*{0,2}GTM Profile:?\*{0,2}\s*/g, "");
  // Collapse excessive blank lines
  cleaned = cleaned.replace(/\n{3,}/g, "\n\n");
  return cleaned.trim();
}

export default function DescribePage() {
  const router = useRouter();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState("");
  const [questionsAnswered, setQuestionsAnswered] = useState(0);
  const [validatedFields, setValidatedFields] = useState<string[]>([]);
  const [isComplete, setIsComplete] = useState(false);
  const [skippedTopics, setSkippedTopics] = useState<string[]>([]);
  const [skipping, setSkipping] = useState(false);
  const [isRestart, setIsRestart] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const sendVoiceRef = useRef<(text: string) => void>(() => {});
  const stt = useSpeechToText(useCallback((text: string) => { if (text.trim()) sendVoiceRef.current(text.trim()); }, []));
  const tts = useTTS();
  const prevMsgCount = useRef(0);

  // Auto-read new ARIA messages aloud
  useEffect(() => {
    if (messages.length > prevMsgCount.current) {
      const last = messages[messages.length - 1];
      if (last?.role === "aria" && tts.enabled) tts.speak(last.text);
    }
    prevMsgCount.current = messages.length;
  }, [messages, tts]);

  useEffect(() => {
    // Detect restart mode (either the reonboarding marker localStorage
    // key set by welcome → "Start from scratch", or an explicit
    // ?restart=1 query param survival hatch).
    const urlRestart =
      typeof window !== "undefined" &&
      new URLSearchParams(window.location.search).get("restart") === "1";
    const restartMode =
      urlRestart ||
      (typeof window !== "undefined" && !!localStorage.getItem("aria_reonboarding_tenant_id"));
    if (restartMode) {
      setIsRestart(true);
    }
    // Pass any existing session_id so the backend can resume from
    // onboarding_drafts (Postgres-backed) instead of creating a fresh
    // session. On restart we skip this entirely and pass restart=true,
    // which forces the backend to delete the prior draft and spin up a
    // clean 8-question agent — otherwise resume picks up the completed
    // prior onboarding and Q1 immediately reports "already complete".
    const existingSessionId =
      !restartMode && typeof window !== "undefined"
        ? localStorage.getItem("aria_onboarding_session") || ""
        : "";
    authFetch(`${API_URL}/api/onboarding/start`, {
      method: "POST",
      body: JSON.stringify({ session_id: existingSessionId, restart: restartMode }),
    })
      .then(r => r.json())
      .then(data => {
        setSessionId(data.session_id);
        // Always persist the (possibly new, possibly existing) session
        // id so subsequent /describe loads can keep resuming.
        if (data.session_id && typeof window !== "undefined") {
          localStorage.setItem("aria_onboarding_session", data.session_id);
        }
        if (data.is_resumed) {
          // Backend rehydrated from the draft. The `message` field
          // here is the LAST assistant turn — which is the question
          // the user was on when they bailed. Prefix a soft welcome-
          // back so they understand the context.
          setMessages([
            {
              role: "aria",
              text:
                "Welcome back — picking up where you left off.\n\n" +
                (data.message || ""),
            },
          ]);
        } else if (data.message) {
          setMessages([{ role: "aria", text: data.message }]);
        }
      })
      .catch(() => {
        // If sessionId is still empty here, /start returned a 401 — session expired.
        // Redirect rather than showing a fake greeting that would break on the next call.
        if (!sessionId && typeof window !== "undefined") {
          console.warn("[describe/start] no session after /start failed, redirecting to /login");
          window.location.href = "/login";
          return;
        }
        setMessages([{ role: "aria", text: "Hi! I'm ARIA, your AI marketing team. I need to ask you 8 quick questions to set up your marketing strategy. Let's start — what did you build?" }]);
      });
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Auto-grow the textarea while voice input is live. onChange-based resize
  // doesn't fire when STT streams transcript via React props.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 240) + "px";
  }, [stt.transcript, stt.listening, input]);

  async function sendMessage(text: string) {
    if (!text || loading || !sessionId) return;
    setInput("");
    setMessages(prev => [...prev, { role: "user", text }]);
    setLoading(true);

    try {
      const res = await authFetch(`${API_URL}/api/onboarding/message`, {
        method: "POST",
        body: JSON.stringify({ session_id: sessionId, message: text }),
      });
      const data = await res.json();
      setMessages(prev => [...prev, { role: "aria", text: sanitizeChatMessage(data.message) }]);
      if (data.validated_fields) setValidatedFields(data.validated_fields);
      if (data.questions_answered != null) setQuestionsAnswered(data.questions_answered);
      if (data.is_complete) {
        setIsComplete(true);
        // Eagerly extract and cache config so /review has data even if the
        // backend session is lost on Railway redeploy before the user navigates.
        localStorage.setItem("aria_onboarding_session", sessionId);
        authFetch(`${API_URL}/api/onboarding/extract-config`, {
          method: "POST",
          body: JSON.stringify({ session_id: sessionId }),
        })
          .then(r => r.ok ? r.json() : null)
          .then(d => {
            if (d?.config) {
              localStorage.setItem("aria_onboarding_config", JSON.stringify(d.config));
            }
          })
          .catch(() => { /* non-blocking */ });
      }
    } catch {
      setMessages(prev => [...prev, { role: "aria", text: "Sorry, I had trouble processing that. Could you try again?" }]);
    }

    setLoading(false);
  }

  sendVoiceRef.current = sendMessage;

  function handleSend(e: React.FormEvent) {
    e.preventDefault();
    sendMessage(input.trim());
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
      const res = await authFetch(`${API_URL}/api/onboarding/skip`, {
        method: "POST",
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
    <div className="flex flex-col h-[calc(100dvh-73px)]">
      {/* ── Mobile sticky progress bar ── */}
      <div className="lg:hidden sticky top-0 z-10 bg-white border-b border-[#E0DED8] px-4 py-2.5 flex items-center gap-3">
        <span className="text-xs font-semibold text-[#534AB7] whitespace-nowrap">
          Q{currentQ} of {totalQuestions}
        </span>
        <div className="flex-1 h-1.5 bg-[#E0DED8] rounded-full">
          <div className="h-full bg-[#534AB7] rounded-full transition-all duration-500" style={{ width: `${progress}%` }} />
        </div>
        <div className="flex gap-1">
          {TOPICS.map((t) => {
            const isSkipped = skippedTopics.includes(t.key);
            const isAnswered = validatedFields.includes(t.key);
            const isActive = !isAnswered && !isSkipped && !isComplete &&
              TOPICS.findIndex(f => !validatedFields.includes(f.key) && !skippedTopics.includes(f.key)) === TOPICS.indexOf(t);
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
                  <img src="/logo.png" alt="ARIA" className="w-8 h-8 rounded-full object-cover flex-shrink-0 mr-3 mt-0.5" />
                )}
                <div>
                  <div className={`max-w-full rounded-2xl px-4 py-3 text-sm leading-relaxed ${
                    msg.role === "user"
                      ? "bg-[#534AB7] text-white rounded-br-md"
                      : "bg-[#F8F8F6] text-[#2C2C2A] border border-[#E0DED8] rounded-bl-md"
                  }`}>
                    {msg.role === "aria" ? renderMarkdown(msg.text) : msg.text}
                  </div>
                  {msg.role === "aria" && tts.supported && (
                    <button
                      onClick={() => tts.speaking ? tts.stop() : tts.speak(msg.text)}
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
              </div>
            ))}

            {loading && (
              <div className="flex justify-start">
                <img src="/logo.png" alt="ARIA" className="w-8 h-8 rounded-full object-cover flex-shrink-0 mr-3" />
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
            {stt.error && (
              <div className="mb-2 flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
                <svg className="w-4 h-4 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                </svg>
                <span className="flex-1">{sttErrorMessage(stt.error)}</span>
                <button type="button" onClick={stt.clearError} className="text-amber-700 hover:text-amber-900 font-medium">
                  Dismiss
                </button>
              </div>
            )}
            {/* Mobile stacks the textarea above its action buttons so
                the input gets full width — on mobile the row layout
                squeezed the textarea to ~60px, rendering placeholder
                text vertically (each letter on its own line). Desktop
                keeps the inline row. The button row uses a separate
                flex container so the 4 actions stay aligned + same
                gap on both breakpoints. */}
            <form onSubmit={handleSend} className="flex flex-col sm:flex-row sm:items-end gap-2 sm:gap-3">
              <textarea
                ref={textareaRef}
                value={stt.listening && stt.transcript ? stt.transcript : input}
                onChange={e => { if (!stt.listening) { setInput(e.target.value); e.target.style.height = "auto"; e.target.style.height = Math.min(e.target.scrollHeight, 240) + "px"; } }}
                onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(e); } }}
                placeholder={stt.listening ? "Listening... (sends after 3s of silence)" : "Type your answer..."}
                disabled={loading}
                rows={1}
                className="flex-1 min-w-0 min-h-[44px] max-h-[240px] rounded-lg border border-[#E0DED8] px-4 py-2.5 text-sm text-[#2C2C2A] placeholder:text-[#B0AFA8] outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7] transition disabled:opacity-60 resize-none"
              />
              <div className="flex items-end gap-2 sm:gap-3">
              {tts.supported && (
                <button
                  type="button"
                  onClick={() => tts.setEnabled(!tts.enabled)}
                  className={`h-11 w-11 flex items-center justify-center rounded-lg transition-colors flex-shrink-0 border border-[#E0DED8] ${
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
                  type="button"
                  onClick={stt.toggle}
                  className={`h-11 w-11 flex items-center justify-center rounded-lg transition-colors flex-shrink-0 ${
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
                className="h-11 px-5 rounded-lg bg-[#534AB7] text-white text-sm font-semibold hover:bg-[#433AA0] transition flex-shrink-0 disabled:opacity-40"
              >
                Send
              </button>
              </div>
            </form>

            {/* Mobile-only Continue / Review button. The right sidebar
                that hosts this CTA on desktop is `hidden lg:block`, so
                without this block phones had no way to advance once
                ARIA said "ready for review". Mirrors the desktop
                button label + disabled rule (>= 3 questions answered),
                but lives inside the chat panel so it's reachable on
                small viewports. lg:hidden hides it on desktop where
                the sidebar version is already visible. */}
            <div className="lg:hidden mt-3">
              <button
                onClick={handleContinue}
                disabled={questionsAnswered < 3}
                className="w-full flex items-center justify-center gap-2 h-11 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#433AA0] transition shadow-sm disabled:opacity-40 disabled:cursor-not-allowed"
              >
                {isComplete ? "Review & finish" : "Continue to review"}
                <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
              </button>
              {questionsAnswered < 3 && (
                <p className="text-[10px] text-[#B0AFA8] text-center mt-2">
                  Answer at least 3 questions to continue
                </p>
              )}
            </div>
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
                {TOPICS.map((topic) => {
                  const isSkipped = skippedTopics.includes(topic.key);
                  const isAnswered = validatedFields.includes(topic.key);
                  const isActive = !isAnswered && !isSkipped && !isComplete &&
                    TOPICS.findIndex(f => !validatedFields.includes(f.key) && !skippedTopics.includes(f.key)) === TOPICS.indexOf(topic);
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
              className="w-full flex items-center justify-center gap-2 h-11 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#433AA0] transition shadow-sm disabled:opacity-40 disabled:cursor-not-allowed"
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
