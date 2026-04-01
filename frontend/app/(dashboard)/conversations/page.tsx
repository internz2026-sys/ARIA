"use client";

import React, { useEffect, useState, useCallback, useRef } from "react";
import { emailThreads, inbox } from "@/lib/api";
import { formatDateAgo } from "@/lib/utils";

interface EmailMessage {
  id: string;
  thread_id: string;
  direction: "inbound" | "outbound";
  sender: string;
  recipients: string;
  subject: string;
  text_body: string;
  html_body: string;
  preview_snippet: string;
  message_timestamp: string;
  approval_status: string;
}

interface EmailThread {
  id: string;
  tenant_id: string;
  gmail_thread_id: string | null;
  contact_email: string;
  subject: string;
  status: string; // open, awaiting_reply, needs_review, replied, closed
  last_message_at: string;
  created_at: string;
}

const STATUS_COLORS: Record<string, { bg: string; text: string; border: string; label: string }> = {
  open: { bg: "bg-blue-50", text: "text-blue-600", border: "border-blue-200", label: "Open" },
  awaiting_reply: { bg: "bg-amber-50", text: "text-amber-700", border: "border-amber-200", label: "Awaiting reply" },
  needs_review: { bg: "bg-red-50", text: "text-red-600", border: "border-red-200", label: "New reply" },
  replied: { bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200", label: "Replied" },
  closed: { bg: "bg-gray-50", text: "text-gray-500", border: "border-gray-200", label: "Closed" },
};

const timeAgo = formatDateAgo;

function extractName(emailStr: string): string {
  const match = emailStr.match(/^(.+?)\s*</);
  if (match) return match[1].replace(/"/g, "").trim();
  return emailStr.split("@")[0];
}

export default function ConversationsPage() {
  const [threads, setThreads] = useState<EmailThread[]>([]);
  const [selected, setSelected] = useState<EmailThread | null>(null);
  const [messages, setMessages] = useState<EmailMessage[]>([]);
  const [loading, setLoading] = useState(true);
  const [threadLoading, setThreadLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [draftLoading, setDraftLoading] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchThreads = useCallback(async () => {
    if (!tenantId) return;
    try {
      // Fetch filtered threads for display
      const data = await emailThreads.list(tenantId, statusFilter);
      setThreads(data.threads || []);

      // Fetch all threads for tab counts (lightweight — reuses cached data if same request)
      const allData = statusFilter ? await emailThreads.list(tenantId) : data;
      const counts: Record<string, number> = { all: 0 };
      for (const t of (allData.threads || [])) {
        counts[t.status] = (counts[t.status] || 0) + 1;
        counts.all++;
      }
      setStatusCounts(counts);
    } catch {
      setThreads([]);
    } finally {
      setLoading(false);
    }
  }, [tenantId, statusFilter]);

  useEffect(() => { fetchThreads(); }, [fetchThreads]);

  // Real-time updates via Socket.IO with fallback polling
  const selectedRef = useRef<EmailThread | null>(null);
  selectedRef.current = selected;
  const socketConnectedRef = useRef(false);

  useEffect(() => {
    if (!tenantId) return;
    let socket: any = null;
    let cleanup: (() => void) | undefined;

    try {
      const { getSocket } = require("@/lib/socket");
      socket = getSocket();
      socketConnectedRef.current = true;

      const handleReply = () => {
        fetchThreads();
        if (selectedRef.current) {
          emailThreads.get(tenantId, selectedRef.current.id).then(data => {
            setMessages(data.messages || []);
          }).catch(() => {});
        }
      };

      socket.on("email_reply_received", handleReply);
      socket.on("email_thread_updated", handleReply);
      socket.on("inbox_item_updated", handleReply);

      cleanup = () => {
        socket.off("email_reply_received", handleReply);
        socket.off("email_thread_updated", handleReply);
        socket.off("inbox_item_updated", handleReply);
      };
    } catch {
      socketConnectedRef.current = false;
    }

    return () => { if (cleanup) cleanup(); };
  }, [tenantId, fetchThreads]);

  // Fallback polling only when socket is unavailable (60s instead of 30s)
  useEffect(() => {
    if (!tenantId || socketConnectedRef.current) return;
    const interval = setInterval(async () => {
      try {
        await emailThreads.sync(tenantId);
        fetchThreads();
        if (selectedRef.current) {
          const data = await emailThreads.get(tenantId, selectedRef.current.id);
          setMessages(data.messages || []);
        }
      } catch {}
    }, 60000);
    return () => clearInterval(interval);
  }, [tenantId, fetchThreads]);

  const selectThread = async (thread: EmailThread) => {
    setSelected(thread);
    setThreadLoading(true);
    try {
      const data = await emailThreads.get(tenantId, thread.id);
      setMessages(data.messages || []);
      // Mark as read if it was needs_review
      if (thread.status === "needs_review") {
        await emailThreads.markRead(tenantId, thread.id);
        setThreads(prev => prev.map(t => t.id === thread.id ? { ...t, status: "open" } : t));
        setSelected({ ...thread, status: "open" });
      }
    } catch {
      setMessages([]);
    }
    setThreadLoading(false);
  };

  const handleSync = async () => {
    if (!tenantId || syncing) return;
    setSyncing(true);
    try {
      await emailThreads.sync(tenantId);
      await fetchThreads();
      if (selected) {
        const data = await emailThreads.get(tenantId, selected.id);
        setMessages(data.messages || []);
      }
    } catch {}
    setSyncing(false);
  };

  const handleDraftReply = async () => {
    if (!tenantId || !selected || draftLoading) return;
    setDraftLoading(true);
    try {
      const result = await emailThreads.draftReply(tenantId, selected.id);
      // Refresh thread messages to show the new draft
      const data = await emailThreads.get(tenantId, selected.id);
      setMessages(data.messages || []);
      await fetchThreads();
    } catch (err: any) {
      alert(err?.message || "Failed to generate draft reply");
    }
    setDraftLoading(false);
  };

  const handleApproveDraft = async (msg: EmailMessage) => {
    // Find the associated inbox item and approve it
    // The draft-reply endpoint creates an inbox item, so we can search for it
    if (!tenantId) return;
    try {
      // List inbox items and find the matching draft
      const inboxData = await inbox.list(tenantId, "draft_pending_approval");
      const items = inboxData.items || [];
      const match = items.find((item: any) =>
        item.email_draft?.reply_to_message_id === msg.id
      );
      if (match) {
        await inbox.approveSend(tenantId, match.id);
        // Refresh
        const data = await emailThreads.get(tenantId, selected!.id);
        setMessages(data.messages || []);
        await fetchThreads();
      } else {
        alert("Could not find the draft inbox item. Try approving from the Inbox page.");
      }
    } catch (err: any) {
      alert(err?.message || "Failed to send. Check Gmail connection in Settings.");
    }
  };

  const handleCancelDraft = async (msg: EmailMessage) => {
    if (!tenantId) return;
    try {
      const inboxData = await inbox.list(tenantId, "draft_pending_approval");
      const items = inboxData.items || [];
      const match = items.find((item: any) =>
        item.email_draft?.reply_to_message_id === msg.id
      );
      if (match) {
        await inbox.cancelDraft(tenantId, match.id);
        const data = await emailThreads.get(tenantId, selected!.id);
        setMessages(data.messages || []);
        await fetchThreads();
      } else {
        alert("Could not find the draft inbox item.");
      }
    } catch (err: any) {
      alert(err?.message || "Failed to cancel draft.");
    }
  };

  const filterTabs = [
    { key: "", label: "All" },
    { key: "needs_review", label: "New replies" },
    { key: "awaiting_reply", label: "Awaiting reply" },
    { key: "open", label: "Open" },
    { key: "closed", label: "Closed" },
  ];

  return (
    <div className="max-w-[1400px] space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-[#2C2C2A]">Conversations</h1>
          <p className="text-sm text-[#5F5E5A]">Email threads with your contacts</p>
        </div>
        <button
          onClick={handleSync}
          disabled={syncing}
          className="flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors disabled:opacity-50"
        >
          <svg className={`w-4 h-4 ${syncing ? "animate-spin" : ""}`} fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182" />
          </svg>
          {syncing ? "Syncing..." : "Sync Gmail"}
        </button>
      </div>

      {/* Filter tabs */}
      <div className="flex items-center gap-1 bg-white rounded-xl border border-[#E0DED8] p-1.5 overflow-x-auto">
        {filterTabs.map(tab => (
          <button
            key={tab.key}
            onClick={() => { setStatusFilter(tab.key); setSelected(null); }}
            className={`px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
              statusFilter === tab.key
                ? "bg-[#EEEDFE] text-[#534AB7]"
                : "text-[#5F5E5A] hover:bg-[#F8F8F6]"
            }`}
          >
            {tab.label}
            {(() => {
              const count = tab.key === "" ? (statusCounts.all || 0) : (statusCounts[tab.key] || 0);
              if (count === 0) return null;
              return (
                <span className={`ml-1.5 text-xs px-1.5 py-0.5 rounded-full ${
                  statusFilter === tab.key ? "bg-[#534AB7] text-white" : "bg-[#F0F0EE] text-[#5F5E5A]"
                }`}>
                  {count}
                </span>
              );
            })()}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[400px] flex items-center justify-center">
          <div className="animate-pulse text-sm text-[#5F5E5A]">Loading conversations...</div>
        </div>
      ) : threads.length === 0 ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[400px] flex items-center justify-center">
          <div className="text-center px-6 py-16">
            <div className="w-16 h-16 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-4">
              <svg className="w-8 h-8 text-[#E0DED8]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
              </svg>
            </div>
            <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No email conversations yet</h3>
            <p className="text-sm text-[#5F5E5A] max-w-sm mx-auto">
              Send an email through ARIA and replies will appear here as conversation threads.
            </p>
          </div>
        </div>
      ) : (
        <div className="flex gap-4 min-h-[600px]">
          {/* Thread list */}
          <div className="w-full md:w-[380px] shrink-0 space-y-2">
            {threads.map(thread => {
              const sc = STATUS_COLORS[thread.status] || STATUS_COLORS.open;
              return (
                <button
                  key={thread.id}
                  onClick={() => selectThread(thread)}
                  className={`w-full text-left p-4 rounded-xl border transition-all ${
                    selected?.id === thread.id
                      ? "border-[#534AB7] bg-[#FAFAFF] shadow-sm"
                      : "border-[#E0DED8] bg-white hover:border-[#C5C3BC]"
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-sm font-semibold text-[#2C2C2A] truncate flex-1">
                      {thread.contact_email}
                    </span>
                    <span className="text-xs text-[#9E9C95] shrink-0">{timeAgo(thread.last_message_at)}</span>
                  </div>
                  <p className="text-sm text-[#5F5E5A] truncate">{thread.subject || "No subject"}</p>
                  <div className="flex items-center gap-2 mt-2">
                    <span className={`text-[11px] px-2 py-0.5 rounded-full border ${sc.bg} ${sc.text} ${sc.border}`}>
                      {sc.label}
                    </span>
                  </div>
                </button>
              );
            })}
          </div>

          {/* Thread detail */}
          <div className="hidden md:flex flex-1 bg-white rounded-xl border border-[#E0DED8] overflow-hidden flex-col">
            {selected ? (
              threadLoading ? (
                <div className="flex items-center justify-center flex-1">
                  <div className="animate-pulse text-sm text-[#5F5E5A]">Loading thread...</div>
                </div>
              ) : (
                <>
                  {/* Thread header */}
                  <div className="border-b border-[#E0DED8] p-5">
                    <div className="flex items-center gap-2 mb-1">
                      <h2 className="text-lg font-semibold text-[#2C2C2A] flex-1 truncate">
                        {selected.subject || "No subject"}
                      </h2>
                      {(() => {
                        const sc = STATUS_COLORS[selected.status] || STATUS_COLORS.open;
                        return (
                          <span className={`text-[11px] px-2.5 py-1 rounded-full border ${sc.bg} ${sc.text} ${sc.border} font-medium shrink-0`}>
                            {sc.label}
                          </span>
                        );
                      })()}
                    </div>
                    <p className="text-sm text-[#5F5E5A]">
                      Conversation with <span className="font-medium text-[#2C2C2A]">{selected.contact_email}</span>
                    </p>
                    <div className="flex items-center gap-2 mt-3">
                      <button
                        onClick={handleDraftReply}
                        disabled={draftLoading}
                        className="flex items-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#4840A0] transition-colors disabled:opacity-60"
                      >
                        <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M9 15L3 9m0 0l6-6M3 9h12a6 6 0 010 12h-3" />
                        </svg>
                        {draftLoading ? "Generating..." : "Generate Reply Draft"}
                      </button>
                    </div>
                  </div>

                  {/* Messages */}
                  <div className="flex-1 overflow-auto p-5 space-y-4">
                    {messages.length === 0 ? (
                      <div className="text-sm text-[#9E9C95] text-center py-8">No messages in this thread</div>
                    ) : messages.map(msg => {
                      const isInbound = msg.direction === "inbound";
                      const isDraft = msg.approval_status === "draft_pending_approval";
                      return (
                        <div key={msg.id} className={`rounded-xl border overflow-hidden ${
                          isInbound
                            ? "border-[#E0DED8] bg-white"
                            : isDraft
                              ? "border-amber-200 bg-amber-50/50"
                              : "border-[#D0CEF0] bg-[#FAFAFF]"
                        }`}>
                          {/* Message header */}
                          <div className={`px-4 py-3 border-b ${
                            isInbound ? "border-[#E0DED8] bg-[#F8F8F6]" : isDraft ? "border-amber-200 bg-amber-50" : "border-[#D0CEF0] bg-[#EEEDFE]"
                          }`}>
                            <div className="flex items-center gap-2">
                              <div className={`w-6 h-6 rounded-full flex items-center justify-center text-[10px] font-bold ${
                                isInbound ? "bg-[#E0DED8] text-[#5F5E5A]" : "bg-[#534AB7] text-white"
                              }`}>
                                {isInbound ? extractName(msg.sender).charAt(0).toUpperCase() : "A"}
                              </div>
                              <div className="flex-1 min-w-0">
                                <span className="text-sm font-medium text-[#2C2C2A]">
                                  {isInbound ? extractName(msg.sender) : "ARIA"}
                                </span>
                                {isDraft && (
                                  <span className="ml-2 text-[11px] px-2 py-0.5 rounded-full bg-amber-100 text-amber-700 border border-amber-200">
                                    Draft — pending approval
                                  </span>
                                )}
                                {msg.approval_status === "sent" && !isInbound && (
                                  <span className="ml-2 text-[11px] px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">
                                    Sent
                                  </span>
                                )}
                              </div>
                              <span className="text-xs text-[#9E9C95] shrink-0">
                                {timeAgo(msg.message_timestamp)}
                              </span>
                            </div>
                            <p className="text-xs text-[#9E9C95] mt-1 truncate">
                              {isInbound ? `From: ${msg.sender}` : `To: ${msg.recipients}`}
                            </p>
                          </div>

                          {/* Message body */}
                          <div className="p-4">
                            {msg.html_body ? (
                              <iframe
                                srcDoc={msg.html_body}
                                title={`Message ${msg.id}`}
                                className="w-full min-h-[120px] border-0"
                                sandbox="allow-same-origin"
                                onLoad={(e) => {
                                  const frame = e.target as HTMLIFrameElement;
                                  if (frame.contentDocument) {
                                    const h = frame.contentDocument.body.scrollHeight;
                                    frame.style.height = `${Math.max(h + 20, 80)}px`;
                                  }
                                }}
                              />
                            ) : (
                              <div className="text-sm text-[#2C2C2A] whitespace-pre-wrap">
                                {msg.text_body || msg.preview_snippet}
                              </div>
                            )}
                          </div>

                          {/* Draft actions */}
                          {isDraft && (
                            <div className="border-t border-amber-200 px-4 py-3 flex items-center gap-2 bg-amber-50/50">
                              <button
                                onClick={() => handleApproveDraft(msg)}
                                className="flex items-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg bg-[#1D9E75] text-white hover:bg-[#178a64] transition-colors"
                              >
                                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                                  <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
                                </svg>
                                Approve & Send
                              </button>
                              <button
                                onClick={() => handleCancelDraft(msg)}
                                className="px-3 py-2 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors"
                              >
                                Cancel draft
                              </button>
                              <span className="text-xs text-[#9E9C95] ml-auto">This draft will be sent to {msg.recipients}</span>
                            </div>
                          )}
                        </div>
                      );
                    })}
                  </div>
                </>
              )
            ) : (
              <div className="flex items-center justify-center flex-1 text-sm text-[#9E9C95]">
                Select a conversation to view the thread
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
