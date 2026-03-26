"use client";

import React, { useEffect, useState, useCallback } from "react";
import { API_URL, inbox } from "@/lib/api";
import { AGENT_NAMES, AGENT_COLORS } from "@/lib/agent-config";

interface InboxItem {
  id: string;
  agent: string;
  type: string;
  title: string;
  content: string;
  status: string;
  priority: string;
  created_at: string;
}

const STATUS_TABS = [
  { key: "", label: "All" },
  { key: "ready", label: "Content ready" },
  { key: "needs_review", label: "Needs review" },
  { key: "completed", label: "Completed" },
];

const TYPE_LABELS: Record<string, string> = {
  blog_post: "Blog Post",
  email_sequence: "Email Sequence",
  social_post: "Social Post",
  ad_campaign: "Ad Campaign",
  strategy_update: "Strategy Update",
  general: "General",
};

const PRIORITY_DOT: Record<string, string> = {
  high: "bg-red-500",
  medium: "bg-amber-400",
  low: "bg-green-500",
};

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export default function InboxPage() {
  const [items, setItems] = useState<InboxItem[]>([]);
  const [activeTab, setActiveTab] = useState("");
  const [selected, setSelected] = useState<InboxItem | null>(null);
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);

  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchItems = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await inbox.list(tenantId, activeTab);
      setItems(data.items || []);
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [tenantId, activeTab]);

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
      const handler = () => {
        fetchItems();
      };
      socket.on("inbox_new_item", handler);
      return () => { socket.off("inbox_new_item", handler); };
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

  const tabCounts = STATUS_TABS.map((tab) => ({
    ...tab,
    count: tab.key ? items.filter((i) => i.status === tab.key).length : items.length,
  }));

  // When filtering by tab, show all items if "All", otherwise filter
  const filteredItems = activeTab ? items.filter((i) => i.status === activeTab) : items;

  return (
    <div className="max-w-[1400px] space-y-4">
      <h1 className="text-2xl font-semibold text-[#2C2C2A]">Inbox</h1>
      <p className="text-sm text-[#5F5E5A] -mt-2">
        Content and deliverables from your marketing agents
      </p>

      {/* Tabs */}
      <div className="flex items-center gap-1 bg-white rounded-xl border border-[#E0DED8] p-1.5 overflow-x-auto">
        {tabCounts.map((tab) => (
          <button
            key={tab.key}
            onClick={() => { setActiveTab(tab.key); setSelected(null); }}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
              activeTab === tab.key
                ? "bg-[#EEEDFE] text-[#534AB7]"
                : "text-[#5F5E5A] hover:bg-[#F8F8F6]"
            }`}
          >
            {tab.label}
            <span
              className={`text-xs px-1.5 py-0.5 rounded-full ${
                activeTab === tab.key
                  ? "bg-[#534AB7] text-white"
                  : "bg-[#F8F8F6] text-[#5F5E5A]"
              }`}
            >
              {tab.count}
            </span>
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
          <div className="w-full md:w-[380px] shrink-0 space-y-2">
            {filteredItems.map((item) => (
              <button
                key={item.id}
                onClick={() => setSelected(item)}
                className={`w-full text-left p-4 rounded-xl border transition-all ${
                  selected?.id === item.id
                    ? "border-[#534AB7] bg-[#FAFAFF] shadow-sm"
                    : "border-[#E0DED8] bg-white hover:border-[#C5C3BC]"
                }`}
              >
                <div className="flex items-center gap-2 mb-1.5">
                  <span
                    className="w-2 h-2 rounded-full shrink-0"
                    style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }}
                  />
                  <span className="text-xs font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
                    {AGENT_NAMES[item.agent] || item.agent}
                  </span>
                  <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
                </div>
                <h4 className="text-sm font-semibold text-[#2C2C2A] truncate">{item.title}</h4>
                <div className="flex items-center gap-2 mt-2">
                  <span className="text-[11px] px-2 py-0.5 rounded-full bg-[#F8F8F6] text-[#5F5E5A] border border-[#E0DED8]">
                    {TYPE_LABELS[item.type] || item.type}
                  </span>
                  <span className={`w-1.5 h-1.5 rounded-full ${PRIORITY_DOT[item.priority] || "bg-gray-400"}`} />
                  <span className="text-[11px] text-[#9E9C95] capitalize">{item.priority}</span>
                  {item.status === "ready" && (
                    <span className="ml-auto text-[11px] px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200">
                      Ready
                    </span>
                  )}
                  {item.status === "completed" && (
                    <span className="ml-auto text-[11px] px-2 py-0.5 rounded-full bg-blue-50 text-blue-600 border border-blue-200">
                      Completed
                    </span>
                  )}
                </div>
              </button>
            ))}
          </div>

          {/* Detail pane */}
          <div className="hidden md:flex flex-1 bg-white rounded-xl border border-[#E0DED8] overflow-hidden">
            {selected ? (
              <div className="flex flex-col w-full">
                {/* Header */}
                <div className="border-b border-[#E0DED8] p-5">
                  <div className="flex items-center gap-2 mb-2">
                    <span
                      className="w-2.5 h-2.5 rounded-full"
                      style={{ backgroundColor: AGENT_COLORS[selected.agent] || "#999" }}
                    />
                    <span className="text-sm font-medium" style={{ color: AGENT_COLORS[selected.agent] || "#999" }}>
                      {AGENT_NAMES[selected.agent] || selected.agent}
                    </span>
                    <span className="text-xs text-[#9E9C95]">{TYPE_LABELS[selected.type] || selected.type}</span>
                    <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(selected.created_at)}</span>
                  </div>
                  <h2 className="text-lg font-semibold text-[#2C2C2A]">{selected.title}</h2>
                  <div className="flex items-center gap-2 mt-3">
                    <button
                      onClick={() => handleCopy(selected.content)}
                      className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#4339A0] transition-colors"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                      </svg>
                      {copied ? "Copied!" : "Copy content"}
                    </button>
                    {selected.status === "ready" && (
                      <button
                        onClick={() => handleStatusChange(selected, "completed")}
                        className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors"
                      >
                        Mark complete
                      </button>
                    )}
                    {selected.status === "completed" && (
                      <button
                        onClick={() => handleStatusChange(selected, "ready")}
                        className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors"
                      >
                        Reopen
                      </button>
                    )}
                    <button
                      onClick={() => handleDelete(selected)}
                      className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors"
                    >
                      Delete
                    </button>
                  </div>
                </div>
                {/* Content */}
                <div className="flex-1 overflow-auto p-5">
                  <div className="prose prose-sm max-w-none text-[#2C2C2A] whitespace-pre-wrap">
                    {selected.content}
                  </div>
                </div>
              </div>
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
