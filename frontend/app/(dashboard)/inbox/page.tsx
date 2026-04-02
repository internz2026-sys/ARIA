"use client";

import React, { useEffect, useState, useCallback, useRef } from "react";
import { API_URL, authFetch, inbox } from "@/lib/api";
import { AGENT_NAMES, AGENT_COLORS } from "@/lib/agent-config";
import EmailEditor from "@/components/shared/EmailEditor";
import { formatDateAgo } from "@/lib/utils";
import { renderMarkdown } from "@/lib/render-markdown";

interface EmailDraft {
  to: string;
  subject: string;
  html_body: string;
  text_body: string;
  preview_snippet: string;
  status: string;
}

interface InboxItem {
  id: string;
  agent: string;
  type: string;
  title: string;
  content: string;
  status: string;
  priority: string;
  created_at: string;
  email_draft?: EmailDraft | null;
}

const STATUS_TABS = [
  { key: "", label: "All" },
  { key: "processing", label: "In progress" },
  { key: "ready", label: "Content ready" },
  { key: "draft_pending_approval", label: "Pending approval" },
  { key: "needs_review", label: "Needs review" },
  { key: "sent", label: "Sent" },
  { key: "completed", label: "Completed" },
];

const TYPE_LABELS: Record<string, string> = {
  blog_post: "Blog Post",
  email_sequence: "Email",
  email_reply: "Email Reply",
  social_post: "Social Post",
  ad_campaign: "Ad Campaign",
  strategy_update: "Strategy Update",
  whatsapp_message: "WhatsApp",
  general: "General",
};

const PRIORITY_DOT: Record<string, string> = {
  high: "bg-red-500",
  medium: "bg-amber-400",
  low: "bg-green-500",
};

const STATUS_BADGES: Record<string, { label: string; bg: string; text: string; border: string }> = {
  processing: { label: "In progress...", bg: "bg-purple-50", text: "text-purple-600", border: "border-purple-200" },
  ready: { label: "Ready", bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200" },
  draft_pending_approval: { label: "Pending approval", bg: "bg-amber-50", text: "text-amber-700", border: "border-amber-200" },
  needs_review: { label: "Needs review", bg: "bg-orange-50", text: "text-orange-600", border: "border-orange-200" },
  sending: { label: "Sending...", bg: "bg-blue-50", text: "text-blue-600", border: "border-blue-200" },
  sent: { label: "Sent", bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200" },
  completed: { label: "Completed", bg: "bg-blue-50", text: "text-blue-600", border: "border-blue-200" },
  failed: { label: "Failed", bg: "bg-red-50", text: "text-red-600", border: "border-red-200" },
  cancelled: { label: "Cancelled", bg: "bg-gray-50", text: "text-gray-500", border: "border-gray-200" },
};

const timeAgo = formatDateAgo;

function stripHtml(html: string): string {
  return html
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n\n")
    .replace(/<\/div>/gi, "\n")
    .replace(/<\/li>/gi, "\n")
    .replace(/<\/h[1-6]>/gi, "\n\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function looksLikeHtml(text: string): boolean {
  return /<\/?[a-z][\s\S]*>/i.test(text);
}

export default function InboxPage() {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [activeTab, setActiveTab] = useState("");
  const [selected, setSelected] = useState<InboxItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);
  const [totalItems, setTotalItems] = useState(0);
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  const PAGE_SIZE = 20;
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchCounts = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await inbox.counts(tenantId);
      setStatusCounts(data.counts || {});
    } catch {}
  }, [tenantId]);

  const fetchItems = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await inbox.list(tenantId, activeTab, page, PAGE_SIZE);
      setItems(data.items || []);
      setTotalPages(data.total_pages || 1);
      setTotalItems(data.total || 0);
      fetchCounts();
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [tenantId, activeTab, page, fetchCounts]);

  useEffect(() => {
    fetchItems();
  }, [fetchItems]);

  // Listen for real-time inbox events via Socket.IO
  useEffect(() => {
    if (!tenantId) return;
    let socket: any = null;
    try {
      const { getSocket } = require("@/lib/socket");
      socket = getSocket();
      const handler = () => { fetchItems(); };
      socket.on("inbox_new_item", handler);
      socket.on("inbox_item_updated", handler);
      return () => { socket.off("inbox_new_item", handler); socket.off("inbox_item_updated", handler); };
    } catch {
      // socket lib may not be available
    }
  }, [tenantId, fetchItems]);

  const handleStatusChange = async (item: InboxItem, newStatus: string) => {
    try {
      await inbox.update(item.id, { status: newStatus });
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, status: newStatus } : i)));
      if (selected?.id === item.id) setSelected({ ...item, status: newStatus });
    } catch {}
  };

  const handleDelete = async (item: InboxItem) => {
    try {
      await inbox.remove(item.id);
      setItems((prev) => prev.filter((i) => i.id !== item.id));
      if (selected?.id === item.id) setSelected(null);
    } catch {}
  };

  const handleCopy = (content: string) => {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleApproveSend = async (item: InboxItem) => {
    if (!tenantId || actionLoading) return;
    setActionLoading("approve");
    try {
      await inbox.approveSend(tenantId, item.id);
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, status: "sent" } : i)));
      if (selected?.id === item.id) setSelected({ ...item, status: "sent" });
    } catch (err: any) {
      alert(err?.message || "Failed to send email. Check Gmail connection in Settings.");
    } finally {
      setActionLoading(null);
    }
  };

  const handleCancelDraft = async (item: InboxItem) => {
    if (!tenantId || actionLoading) return;
    setActionLoading("cancel");
    try {
      await inbox.cancelDraft(tenantId, item.id);
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, status: "cancelled" } : i)));
      if (selected?.id === item.id) setSelected({ ...item, status: "cancelled" });
    } catch {}
    setActionLoading(null);
  };

  const handlePublishSocial = async (item: InboxItem) => {
    if (!tenantId || actionLoading) return;
    setActionLoading("publish");
    try {
      const res = await inbox.approvePublishSocial(tenantId, item.id);
      const newStatus = res.status === "sent" ? "sent" : "failed";
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, status: newStatus } : i)));
      if (selected?.id === item.id) setSelected({ ...item, status: newStatus });
      if (newStatus === "failed") {
        alert("Failed to publish. Check your Twitter connection in Settings.");
      }
    } catch (err: any) {
      alert(err?.message || "Failed to publish to X. Check connection in Settings.");
    } finally {
      setActionLoading(null);
    }
  };

  const handlePublishLinkedIn = async (item: InboxItem) => {
    if (!tenantId || actionLoading) return;
    setActionLoading("linkedin");
    try {
      // Extract LinkedIn-specific text from social post content
      const posts = parseSocialPosts(item.content);
      let text = "";
      if (posts.length > 0) {
        // Prefer the LinkedIn post; fall back to first post
        const post = posts.find(p => p.platform?.toLowerCase() === "linkedin") || posts[0];
        text = post.text || "";
        const hashtags = post.hashtags || [];
        if (hashtags.length > 0) {
          const tagStr = hashtags.map((t: string) => `#${t.replace(/^#/, "")}`).join(" ");
          if (!text.includes(tagStr)) text = `${text}\n\n${tagStr}`;
        }
      } else {
        text = item.content;
      }

      const res = await authFetch(`${API_URL}/api/linkedin/${tenantId}/post`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Failed (${res.status})`);
      }
      alert("Published to LinkedIn!");
    } catch (err: any) {
      alert(err?.message || "Failed to publish to LinkedIn. Check connection in Settings.");
    } finally {
      setActionLoading(null);
    }
  };

  const [waReplyText, setWaReplyText] = useState("");
  const [waReplying, setWaReplying] = useState(false);

  const handleWhatsAppReply = async (item: InboxItem) => {
    if (!tenantId || !waReplyText.trim()) return;
    setWaReplying(true);
    try {
      const meta = typeof item.content === "string" ? {} : {};
      // Parse from_number from item metadata stored in content title
      const fromMatch = item.title.match(/\+?\d{10,15}/);
      const toNumber = fromMatch?.[0] || "";
      if (!toNumber) { alert("Cannot determine recipient number"); return; }

      const res = await authFetch(`${API_URL}/api/whatsapp/${tenantId}/send`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to: toNumber, message: waReplyText }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Send failed (${res.status})`);
      }
      setWaReplyText("");
      alert("WhatsApp reply sent!");
    } catch (err: any) {
      alert(err?.message || "Failed to send WhatsApp reply");
    } finally {
      setWaReplying(false);
    }
  };

  const isEmailDraft = (item: InboxItem) => !!item.email_draft;
  const isSocialPost = (item: InboxItem) => item.type === "social_post";
  const isPendingApproval = (item: InboxItem) => item.status === "draft_pending_approval";

  const filteredItems = items;

  // ─── Bulk actions ───
  const toggleCheck = (id: string) => {
    setCheckedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAllChecked = () => {
    if (checkedIds.size === filteredItems.length) {
      setCheckedIds(new Set());
    } else {
      setCheckedIds(new Set(filteredItems.map((i) => i.id)));
    }
  };

  const handleBulkDelete = async () => {
    if (checkedIds.size === 0) return;
    setActionLoading("bulk-delete");
    try {
      await Promise.all(Array.from(checkedIds).map((id) => inbox.remove(id)));
      setItems((prev) => prev.filter((i) => !checkedIds.has(i.id)));
      if (selected && checkedIds.has(selected.id)) setSelected(null);
      setCheckedIds(new Set());
    } catch {}
    setActionLoading(null);
  };

  const handleBulkComplete = async () => {
    if (checkedIds.size === 0) return;
    setActionLoading("bulk-complete");
    try {
      await Promise.all(Array.from(checkedIds).map((id) => inbox.update(id, { status: "completed" })));
      setItems((prev) =>
        prev.map((i) => (checkedIds.has(i.id) ? { ...i, status: "completed" } : i))
      );
      if (selected && checkedIds.has(selected.id)) setSelected({ ...selected, status: "completed" });
      setCheckedIds(new Set());
    } catch {}
    setActionLoading(null);
  };

  // ─── Save draft edits ───
  const handleSaveDraft = async (item: InboxItem, data: { to: string; subject: string; html_body: string }) => {
    if (!tenantId) return;
    try {
      const result = await inbox.updateDraft(tenantId, item.id, data);
      // Update local state with saved draft
      const updatedDraft = result.email_draft || { ...item.email_draft, ...data };
      setItems((prev) =>
        prev.map((i) => (i.id === item.id ? { ...i, email_draft: updatedDraft } : i))
      );
      if (selected?.id === item.id) {
        setSelected({ ...item, email_draft: updatedDraft });
      }
    } catch (err: any) {
      alert(err?.message || "Failed to save changes");
      throw err;
    }
  };

  // ─── Email Draft Editor (editable) ───
  const renderEmailEditor = (item: InboxItem) => {
    const draft = item.email_draft!;
    return (
      <EmailEditor
        key={item.id}
        to={draft.to || ""}
        subject={draft.subject || ""}
        htmlBody={draft.html_body || ""}
        onSave={(data) => handleSaveDraft(item, data)}
        onSend={() => handleApproveSend(item)}
        onCancel={() => handleCancelDraft(item)}
        sendDisabled={actionLoading === "approve"}
        sendLoading={actionLoading === "approve"}
        cancelLoading={actionLoading === "cancel"}
      />
    );
  };

  // ─── Email Draft Read-Only View (sent/failed/cancelled) ───
  const renderEmailReadOnly = (item: InboxItem) => {
    const draft = item.email_draft!;
    return (
      <div className="flex flex-col w-full">
        <div className="border-b border-[#E0DED8] p-5">
          <div className="flex items-center gap-2 mb-3">
            <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }} />
            <span className="text-sm font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
              {AGENT_NAMES[item.agent] || item.agent}
            </span>
            <span className="text-xs text-[#9E9C95]">Email</span>
            <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
          </div>
          <div className="bg-[#F8F8F6] rounded-lg p-4 space-y-2 mb-3">
            <div className="flex items-baseline gap-2">
              <span className="text-xs font-semibold text-[#5F5E5A] uppercase w-16 shrink-0">To</span>
              <span className="text-sm text-[#2C2C2A]">{draft.to || "—"}</span>
            </div>
            <div className="flex items-baseline gap-2">
              <span className="text-xs font-semibold text-[#5F5E5A] uppercase w-16 shrink-0">Subject</span>
              <span className="text-sm font-medium text-[#2C2C2A]">{draft.subject}</span>
            </div>
          </div>
          {(() => {
            const badge = STATUS_BADGES[item.status];
            return badge ? (
              <span className={`inline-flex items-center text-[11px] px-2.5 py-1 rounded-full border ${badge.bg} ${badge.text} ${badge.border} font-medium`}>
                {badge.label}
              </span>
            ) : null;
          })()}
        </div>
        {/* Action bar — above content */}
        <div className="border-b border-[#E0DED8] px-5 py-3 flex items-center gap-2 bg-[#F8F8F6]">
          {item.status === "sent" && (
            <span className="flex items-center gap-1.5 text-sm font-medium text-[#1D9E75]">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              Email sent successfully
            </span>
          )}
          {item.status === "failed" && (
            <>
              <span className="flex items-center gap-1.5 text-sm font-medium text-red-500">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                </svg>
                Send failed
              </span>
              <button
                onClick={() => {
                  setItems(prev => prev.map(i => i.id === item.id ? { ...i, status: "draft_pending_approval" } : i));
                  setSelected({ ...item, status: "draft_pending_approval" });
                }}
                className="px-3 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] bg-white text-[#5F5E5A] hover:bg-[#F0EFEC] transition-colors"
              >
                Edit & Retry
              </button>
            </>
          )}
          <button
            onClick={() => handleDelete(item)}
            className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors"
          >
            Delete
          </button>
        </div>
        {/* Email content */}
        <div className="flex-1 overflow-auto p-5">
          <div className="bg-white rounded-lg border border-[#E0DED8] overflow-hidden">
            <iframe
              ref={iframeRef}
              srcDoc={draft.html_body}
              title="Email preview"
              className="w-full min-h-[300px] border-0"
              sandbox="allow-same-origin"
              onLoad={() => {
                if (iframeRef.current?.contentDocument) {
                  const h = iframeRef.current.contentDocument.body.scrollHeight;
                  iframeRef.current.style.height = `${Math.max(h + 20, 300)}px`;
                }
              }}
            />
          </div>
        </div>
      </div>
    );
  };

  // ─── Parse social posts from JSON content ───
  const parseSocialPosts = (content: string): { platform: string; text: string; hashtags?: string[] }[] => {
    try {
      const start = content.indexOf("{");
      const end = content.lastIndexOf("}") + 1;
      if (start >= 0 && end > start) {
        const data = JSON.parse(content.substring(start, end));
        if (data.posts && Array.isArray(data.posts)) return data.posts;
      }
    } catch {}
    try {
      const start = content.indexOf("[");
      const end = content.lastIndexOf("]") + 1;
      if (start >= 0 && end > start) return JSON.parse(content.substring(start, end));
    } catch {}
    return [];
  };

  // ─── Social post detail view (tweet cards) ───
  const renderSocialDetail = (item: InboxItem) => {
    const posts = parseSocialPosts(item.content);
    const PLATFORM_ICONS: Record<string, React.ReactNode> = {
      twitter: <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" /></svg>,
      linkedin: <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" /></svg>,
      facebook: <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z" /></svg>,
    };
    const PLATFORM_COLORS: Record<string, { bg: string; border: string; iconBg: string }> = {
      twitter: { bg: "bg-white", border: "border-gray-200", iconBg: "bg-black text-white" },
      linkedin: { bg: "bg-white", border: "border-blue-100", iconBg: "bg-[#0A66C2] text-white" },
      facebook: { bg: "bg-white", border: "border-blue-100", iconBg: "bg-[#1877F2] text-white" },
    };
    const PLATFORM_NAMES: Record<string, string> = { twitter: "X / Twitter", linkedin: "LinkedIn", facebook: "Facebook" };

    return (
      <div className="flex flex-col w-full">
        <div className="border-b border-[#E0DED8] p-5">
          <div className="flex items-center gap-2 mb-2">
            <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }} />
            <span className="text-sm font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
              {AGENT_NAMES[item.agent] || item.agent}
            </span>
            <span className="text-xs text-[#9E9C95]">Social Post</span>
            <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
          </div>
          <h2 className="text-lg font-semibold text-[#2C2C2A]">{item.title}</h2>
          <div className="flex items-center gap-2 mt-3 flex-wrap">
            {isSocialPost(item) && (item.status === "ready" || item.status === "needs_review" || item.status === "failed") && (
              <button
                onClick={() => handlePublishSocial(item)}
                disabled={actionLoading === "publish"}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-black text-white hover:bg-gray-800 transition-colors disabled:opacity-60"
              >
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
                </svg>
                {actionLoading === "publish" ? "Publishing..." : item.status === "failed" ? "Retry Publish" : "Publish to X"}
              </button>
            )}
            {isSocialPost(item) && (item.status === "ready" || item.status === "needs_review" || item.status === "failed") && (
              <button
                onClick={() => handlePublishLinkedIn(item)}
                disabled={actionLoading === "linkedin"}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#0A66C2] text-white hover:bg-[#084d93] transition-colors disabled:opacity-60"
              >
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" />
                </svg>
                {actionLoading === "linkedin" ? "Publishing..." : "Publish to LinkedIn"}
              </button>
            )}
            {isSocialPost(item) && item.status === "sent" && (
              <span className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
                Published
              </span>
            )}
            {item.status === "ready" && (
              <button onClick={() => handleStatusChange(item, "completed")} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors">
                Mark complete
              </button>
            )}
            <button onClick={() => handleDelete(item)} className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors">
              Delete
            </button>
          </div>
        </div>
        <div className="flex-1 overflow-auto p-5 space-y-4">
          {posts.length > 0 ? posts.map((post, idx) => {
            const platform = (post.platform || "twitter").toLowerCase();
            const colors = PLATFORM_COLORS[platform] || PLATFORM_COLORS.twitter;
            const hashtags = post.hashtags || [];
            const charLimit = platform === "twitter" ? 280 : platform === "linkedin" ? 3000 : 2000;
            const textWithTags = hashtags.length > 0
              ? `${post.text}${post.text.includes("#") ? "" : "\n" + hashtags.map(t => `#${t.replace(/^#/, "")}`).join(" ")}`
              : post.text;

            return (
              <div key={idx} className={`rounded-xl border ${colors.border} ${colors.bg} overflow-hidden shadow-sm`}>
                {/* Platform header */}
                <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-100 bg-gray-50/50">
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center ${colors.iconBg}`}>
                    {PLATFORM_ICONS[platform] || PLATFORM_ICONS.twitter}
                  </div>
                  <div>
                    <span className="text-sm font-semibold text-[#2C2C2A]">{PLATFORM_NAMES[platform] || platform}</span>
                    <span className="text-xs text-[#9E9C95] ml-2">{post.text.length}/{charLimit} chars</span>
                    {platform === "twitter" && post.text.length > 280 && (
                      <span className="text-xs text-red-500 ml-1 font-medium">Over limit!</span>
                    )}
                  </div>
                  <button
                    onClick={() => { navigator.clipboard.writeText(textWithTags); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
                    className="ml-auto p-1.5 rounded-lg hover:bg-gray-100 text-[#9E9C95] hover:text-[#2C2C2A] transition-colors"
                    title="Copy post"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                  </button>
                </div>
                {/* Post content */}
                <div className="px-4 py-4">
                  <p className="text-[15px] text-[#0F1419] leading-relaxed whitespace-pre-wrap">{post.text}</p>
                  {hashtags.length > 0 && !post.text.includes("#") && (
                    <div className="flex flex-wrap gap-1.5 mt-3">
                      {hashtags.map((tag, i) => (
                        <span key={i} className="text-sm text-[#1d9bf0] font-medium">
                          #{tag.replace(/^#/, "")}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
                {/* Footer with engagement placeholders */}
                <div className="flex items-center gap-8 px-4 py-2.5 border-t border-gray-100 text-[#536471]">
                  <span className="flex items-center gap-1.5 text-xs">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 20.25c4.97 0 9-3.694 9-8.25s-4.03-8.25-9-8.25S3 7.444 3 12c0 2.104.859 4.023 2.273 5.48.432.447.74 1.04.586 1.641a4.483 4.483 0 01-.923 1.785A5.969 5.969 0 006 21c1.282 0 2.47-.402 3.445-1.087.81.22 1.668.337 2.555.337z" /></svg>
                    Reply
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 12c0-1.232-.046-2.453-.138-3.662a4.006 4.006 0 00-3.7-3.7 48.678 48.678 0 00-7.324 0 4.006 4.006 0 00-3.7 3.7c-.017.22-.032.441-.046.662M19.5 12l3-3m-3 3l-3-3m-12 3c0 1.232.046 2.453.138 3.662a4.006 4.006 0 003.7 3.7 48.656 48.656 0 007.324 0 4.006 4.006 0 003.7-3.7c.017-.22.032-.441.046-.662M4.5 12l3 3m-3-3l-3 3" /></svg>
                    Repost
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" /></svg>
                    Like
                  </span>
                </div>
              </div>
            );
          }) : (
            <div className="prose prose-sm max-w-none text-[#2C2C2A] whitespace-pre-wrap">
              {item.content}
            </div>
          )}
        </div>
      </div>
    );
  };

  // ─── Standard (non-email) detail view ───
  const renderWhatsAppDetail = (item: InboxItem) => (
    <div className="flex flex-col w-full">
      <div className="border-b border-[#E0DED8] p-5">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-6 h-6 rounded-full bg-[#25D366] flex items-center justify-center">
            <svg className="w-3.5 h-3.5 text-white" viewBox="0 0 24 24" fill="currentColor">
              <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z" />
            </svg>
          </div>
          <span className="text-sm font-medium text-[#25D366]">WhatsApp</span>
          <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
        </div>
        <h2 className="text-lg font-semibold text-[#2C2C2A]">{item.title}</h2>
      </div>
      {/* Message bubble */}
      <div className="flex-1 overflow-auto p-5">
        <div className="max-w-md">
          <div className="bg-[#E8F5E8] rounded-xl rounded-tl-sm px-4 py-3 mb-4">
            <p className="text-sm text-[#2C2C2A] whitespace-pre-wrap">{item.content}</p>
            <p className="text-[10px] text-[#5F5E5A] mt-1 text-right">{timeAgo(item.created_at)}</p>
          </div>
        </div>
      </div>
      {/* Reply box */}
      <div className="border-t border-[#E0DED8] p-4">
        <div className="flex items-end gap-2">
          <textarea
            value={waReplyText}
            onChange={e => setWaReplyText(e.target.value)}
            placeholder="Type a reply..."
            rows={2}
            className="flex-1 px-3 py-2 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] resize-none focus:outline-none focus:ring-2 focus:ring-[#25D366]/20 focus:border-[#25D366]"
          />
          <button
            onClick={() => handleWhatsAppReply(item)}
            disabled={waReplying || !waReplyText.trim()}
            className="px-4 py-2 bg-[#25D366] text-white rounded-lg text-sm font-medium hover:bg-[#1da851] transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
            </svg>
            {waReplying ? "Sending..." : "Reply"}
          </button>
        </div>
      </div>
    </div>
  );

  const renderStandardDetail = (item: InboxItem) => (
    <div className="flex flex-col w-full">
      <div className="border-b border-[#E0DED8] p-5">
        <div className="flex items-center gap-2 mb-2">
          <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }} />
          <span className="text-sm font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
            {AGENT_NAMES[item.agent] || item.agent}
          </span>
          <span className="text-xs text-[#9E9C95]">{TYPE_LABELS[item.type] || item.type}</span>
          <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
        </div>
        <h2 className="text-lg font-semibold text-[#2C2C2A]">{item.title}</h2>
        <div className="flex items-center gap-2 mt-3">
          <button
            onClick={() => handleCopy(item.content)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#4339A0] transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
            </svg>
            {copied ? "Copied!" : "Copy content"}
          </button>
          {item.status === "ready" && (
            <button onClick={() => handleStatusChange(item, "completed")} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors">
              Mark complete
            </button>
          )}
          {item.status === "completed" && (
            <button onClick={() => handleStatusChange(item, "ready")} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors">
              Reopen
            </button>
          )}
          <button onClick={() => handleDelete(item)} className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors">
            Delete
          </button>
        </div>
      </div>
      <div className="flex-1 overflow-auto p-5">
        <div className="prose prose-sm max-w-none text-[#2C2C2A]">
          {looksLikeHtml(item.content)
            ? <div className="whitespace-pre-wrap">{stripHtml(item.content)}</div>
            : item.content.includes("## ") || item.content.includes("**")
              ? renderMarkdown(item.content)
              : <div className="whitespace-pre-wrap">{item.content}</div>
          }
        </div>
      </div>
    </div>
  );

  return (
    <div className="max-w-[1400px] space-y-4">
      <h1 className="text-2xl font-semibold text-[#2C2C2A]">Inbox</h1>
      <p className="text-sm text-[#5F5E5A] -mt-2">
        Content and deliverables from your marketing agents
      </p>

      {/* Tabs */}
      <div className="flex items-center gap-1 bg-white rounded-xl border border-[#E0DED8] p-1.5 overflow-x-auto">
        {STATUS_TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => { setActiveTab(tab.key); setSelected(null); setCheckedIds(new Set()); setPage(1); }}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
              activeTab === tab.key
                ? "bg-[#EEEDFE] text-[#534AB7]"
                : "text-[#5F5E5A] hover:bg-[#F8F8F6]"
            }`}
          >
            {tab.label}
            {(() => {
              const count = tab.key === "" ? (statusCounts.all || 0) : (statusCounts[tab.key] || 0);
              if (count === 0) return null;
              return (
                <span className={`text-xs px-1.5 py-0.5 rounded-full ${
                  activeTab === tab.key ? "bg-[#534AB7] text-white" : "bg-[#F0F0EE] text-[#5F5E5A]"
                }`}>
                  {count}
                </span>
              );
            })()}
          </button>
        ))}
      </div>

      {/* Content */}
      {loading ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[400px] flex items-center justify-center">
          <div className="animate-pulse text-sm text-[#5F5E5A]">Loading inbox...</div>
        </div>
      ) : filteredItems.length === 0 ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[400px] flex items-center justify-center">
          <div className="text-center px-6 py-16">
            <div className="w-16 h-16 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-4">
              <svg className="w-8 h-8 text-[#E0DED8]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859M12 3v8.25m0 0l-3-3m3 3l3-3" />
              </svg>
            </div>
            <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No deliverables yet</h3>
            <p className="text-sm text-[#5F5E5A] max-w-sm mx-auto">
              Ask the CEO agent to create content — blog posts, emails, social posts, or ad campaigns will appear here.
            </p>
            <a href="/chat" className="inline-block mt-4 text-sm font-medium text-[#534AB7] hover:underline">
              Chat with CEO to get started
            </a>
          </div>
        </div>
      ) : (
        <div className="flex gap-4 min-h-[500px]">
          {/* Item list */}
          <div className="w-full md:w-[380px] shrink-0 flex flex-col gap-2">
            {/* Bulk action toolbar */}
            <div className="flex items-center gap-2 px-1">
              <label className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={filteredItems.length > 0 && checkedIds.size === filteredItems.length}
                  onChange={toggleAllChecked}
                  className="w-4 h-4 rounded border-[#C5C3BC] text-[#534AB7] focus:ring-[#534AB7] cursor-pointer"
                />
                <span className="text-xs text-[#5F5E5A]">
                  {checkedIds.size > 0 ? `${checkedIds.size} selected` : "Select all"}
                </span>
              </label>
              {checkedIds.size > 0 && (
                <div className="flex items-center gap-1.5 ml-auto">
                  <button
                    onClick={handleBulkComplete}
                    disabled={actionLoading === "bulk-complete"}
                    className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#4339A0] transition-colors disabled:opacity-60"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                    </svg>
                    {actionLoading === "bulk-complete" ? "Updating..." : "Mark completed"}
                  </button>
                  <button
                    onClick={handleBulkDelete}
                    disabled={actionLoading === "bulk-delete"}
                    className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors disabled:opacity-60"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                    </svg>
                    {actionLoading === "bulk-delete" ? "Deleting..." : "Delete"}
                  </button>
                </div>
              )}
            </div>

            {filteredItems.map((item) => {
              const badge = STATUS_BADGES[item.status];
              const isChecked = checkedIds.has(item.id);
              return (
                <div
                  key={item.id}
                  className={`flex items-start gap-2 p-4 rounded-xl border transition-all cursor-pointer ${
                    selected?.id === item.id
                      ? "border-[#534AB7] bg-[#FAFAFF] shadow-sm"
                      : isChecked
                      ? "border-[#534AB7]/40 bg-[#FAFAFF]/50"
                      : "border-[#E0DED8] bg-white hover:border-[#C5C3BC]"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={isChecked}
                    onChange={(e) => { e.stopPropagation(); toggleCheck(item.id); }}
                    className="w-4 h-4 mt-0.5 rounded border-[#C5C3BC] text-[#534AB7] focus:ring-[#534AB7] cursor-pointer shrink-0"
                  />
                  <button
                    onClick={() => { setSelected(item); }}
                    className="flex-1 text-left min-w-0"
                  >
                    <div className="flex items-center gap-2 mb-1.5">
                      <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }} />
                      <span className="text-xs font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
                        {AGENT_NAMES[item.agent] || item.agent}
                      </span>
                      <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
                    </div>
                    <h4 className="text-sm font-semibold text-[#2C2C2A] truncate">{item.title}</h4>
                    {item.email_draft?.preview_snippet && (
                      <p className="text-xs text-[#9E9C95] mt-1 line-clamp-2">{item.email_draft.preview_snippet}</p>
                    )}
                    <div className="flex items-center gap-2 mt-2">
                      <span className="text-[11px] px-2 py-0.5 rounded-full bg-[#F8F8F6] text-[#5F5E5A] border border-[#E0DED8]">
                        {TYPE_LABELS[item.type] || item.type}
                      </span>
                      <span className={`w-1.5 h-1.5 rounded-full ${PRIORITY_DOT[item.priority] || "bg-gray-400"}`} />
                      <span className="text-[11px] text-[#9E9C95] capitalize">{item.priority}</span>
                      {badge && (
                        <span className={`ml-auto text-[11px] px-2 py-0.5 rounded-full border ${badge.bg} ${badge.text} ${badge.border}`}>
                          {badge.label}
                        </span>
                      )}
                    </div>
                  </button>
                </div>
              );
            })}

            {/* Pagination */}
            {totalPages > 1 && (
              <div className="flex items-center justify-between pt-2 px-1">
                <span className="text-xs text-[#9E9C95]">
                  Page {page} of {totalPages} ({totalItems} items)
                </span>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => { setPage(1); setCheckedIds(new Set()); }}
                    disabled={page <= 1}
                    className="px-2 py-1 text-xs rounded-md border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    First
                  </button>
                  <button
                    onClick={() => { setPage((p) => Math.max(1, p - 1)); setCheckedIds(new Set()); }}
                    disabled={page <= 1}
                    className="px-2 py-1 text-xs rounded-md border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
                    </svg>
                  </button>
                  <button
                    onClick={() => { setPage((p) => Math.min(totalPages, p + 1)); setCheckedIds(new Set()); }}
                    disabled={page >= totalPages}
                    className="px-2 py-1 text-xs rounded-md border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
                    </svg>
                  </button>
                  <button
                    onClick={() => { setPage(totalPages); setCheckedIds(new Set()); }}
                    disabled={page >= totalPages}
                    className="px-2 py-1 text-xs rounded-md border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
                  >
                    Last
                  </button>
                </div>
              </div>
            )}
          </div>

          {/* Detail pane */}
          <div className="hidden md:flex flex-1 bg-white rounded-xl border border-[#E0DED8] overflow-hidden">
            {selected ? (
              isEmailDraft(selected)
                ? (isPendingApproval(selected) ? renderEmailEditor(selected) : renderEmailReadOnly(selected))
                : isSocialPost(selected) ? renderSocialDetail(selected)
                : selected.type === "whatsapp_message" ? renderWhatsAppDetail(selected)
                : renderStandardDetail(selected)
            ) : (
              <div className="flex items-center justify-center w-full text-sm text-[#9E9C95]">
                Select an item to view its content
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
