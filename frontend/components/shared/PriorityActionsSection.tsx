"use client";

import React, { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { authFetch, API_URL } from "@/lib/api";
import { useNotifications } from "@/lib/use-notifications";

// Priority Actions — pinned section at the top of the Projects page that
// surfaces inbox drafts older than 24h that the user hasn't acted on.
// Per the Stagnation Monitor spec, each row is a draft (email/social/blog)
// that's been sitting in needs_review or draft_pending_approval. The
// component hides itself entirely when there are no stale rows so the
// Projects page header stays clean for active users.
//
// Deep-link: clicking a row navigates to /inbox?id=<uuid> which the
// inbox page already handles (auto-selects + opens the detail pane).

type StaleItem = {
  id: string;
  agent: string;
  type: string;
  title: string;
  status: string;
  priority: string;
  created_at: string;
  snoozed_until?: string | null;
};

type StaleResponse = {
  stale_items: StaleItem[];
  stale_count: number;
  recent_count: number;
  is_buried: boolean;
  hours_threshold: number;
};

const AGENT_LABEL: Record<string, string> = {
  ceo: "ARIA CEO",
  content_writer: "Content Writer",
  email_marketer: "Email Marketer",
  social_manager: "Social Manager",
  ad_strategist: "Ad Strategist",
  media: "Media Designer",
};

function ageString(createdAt: string): string {
  if (!createdAt) return "—";
  const ms = Date.now() - new Date(createdAt).getTime();
  const hours = Math.max(0, Math.floor(ms / 3_600_000));
  if (hours >= 48) return `${Math.floor(hours / 24)}d ago`;
  return `${hours}h ago`;
}

function ageColor(createdAt: string): string {
  // Subtle color shift on age — newly stale (24-48h) is amber, very
  // old (>48h) is red. Per spec acceptance criteria: "UI highlights
  // 'Old' pending tasks differently than 'New' ones".
  if (!createdAt) return "text-[#8A6D00]";
  const ms = Date.now() - new Date(createdAt).getTime();
  const hours = Math.max(0, Math.floor(ms / 3_600_000));
  if (hours >= 72) return "text-[#B8491F]";
  if (hours >= 48) return "text-[#D85A30]";
  return "text-[#8A6D00]";
}

export default function PriorityActionsSection() {
  const router = useRouter();
  const { showToast } = useNotifications();
  const [data, setData] = useState<StaleResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [snoozing, setSnoozing] = useState<string | null>(null);
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchStale = useCallback(async () => {
    if (!tenantId) { setLoading(false); return; }
    try {
      const res = await authFetch(`${API_URL}/api/projects/stale/${tenantId}`);
      if (res.ok) {
        const json = (await res.json()) as StaleResponse;
        setData(json);
      }
    } catch {
      // Silent fail — Projects page still loads its main Kanban
    } finally {
      setLoading(false);
    }
  }, [tenantId]);

  useEffect(() => {
    fetchStale();
  }, [fetchStale]);

  const handleOpen = (item: StaleItem) => {
    router.push(`/inbox?id=${encodeURIComponent(item.id)}`);
  };

  const handleSnooze = async (item: StaleItem) => {
    if (!tenantId) return;
    setSnoozing(item.id);
    try {
      const res = await authFetch(`${API_URL}/api/projects/${tenantId}/snooze/${item.id}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hours: 24 }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Snooze failed (${res.status})`);
      }
      // Optimistic — drop the row from the local state. The backend
      // has set snoozed_until, so a refetch would also drop it but
      // optimistic feels snappier.
      setData((prev) => {
        if (!prev) return prev;
        const next_items = prev.stale_items.filter((x) => x.id !== item.id);
        return {
          ...prev,
          stale_items: next_items,
          stale_count: next_items.length,
          is_buried: next_items.length > 0 && prev.recent_count >= 5,
        };
      });
      showToast({
        title: "Snoozed for 24h",
        body: `${item.title?.slice(0, 60) || "Item"} will reappear tomorrow.`,
        variant: "success",
      });
    } catch (e: any) {
      showToast({
        title: "Couldn't snooze",
        body: e?.message || "Try again in a moment.",
        variant: "error",
      });
    } finally {
      setSnoozing(null);
    }
  };

  // Hide the entire section when there's nothing to surface — keeps
  // the Projects page clean for users with no buried work.
  if (loading || !data || data.stale_count === 0) return null;

  const isBuried = data.is_buried;

  return (
    <div className={`mb-6 rounded-xl border ${isBuried ? "border-[#D85A30]/40 bg-[#FDF3EE]" : "border-[#D4B24C]/40 bg-[#FFFAEC]"}`}>
      <div className="flex items-start gap-3 px-4 py-3 border-b border-[#E0DED8]/40">
        <div className={`mt-0.5 ${isBuried ? "text-[#B8491F]" : "text-[#8A6D00]"}`}>
          {/* Stale-task icon — clock with an exclamation */}
          <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l2 2m6-2a8 8 0 11-16 0 8 8 0 0116 0z" />
          </svg>
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h2 className="text-sm font-bold text-[#2C2C2A]">Priority Actions</h2>
            <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${isBuried ? "bg-[#D85A30] text-white" : "bg-[#D4B24C] text-white"}`}>
              {data.stale_count} pending
            </span>
            {isBuried && (
              <span className="text-[10px] font-medium text-[#B8491F]">
                buried by {data.recent_count} newer items
              </span>
            )}
          </div>
          <p className="text-xs text-[#5F5E5A] mt-0.5">
            Drafts that have been waiting on you for more than {data.hours_threshold} hours.
          </p>
        </div>
      </div>

      <div className="divide-y divide-[#E0DED8]/40">
        {data.stale_items.map((item) => (
          <div
            key={item.id}
            data-stale-row={item.id}
            className="flex items-center gap-3 px-4 py-3 hover:bg-white/60 transition-colors"
          >
            <button
              onClick={() => handleOpen(item)}
              className="flex-1 min-w-0 text-left cursor-pointer"
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span className={`text-xs font-bold ${ageColor(item.created_at)}`}>
                  {ageString(item.created_at)}
                </span>
                <span className="text-[10px] font-medium text-[#5F5E5A] uppercase tracking-wide">
                  {AGENT_LABEL[item.agent] || item.agent}
                </span>
                <span className="text-[10px] font-medium text-[#9E9C95] uppercase tracking-wide">
                  · {item.status.replace(/_/g, " ")}
                </span>
              </div>
              <p className="text-sm text-[#2C2C2A] mt-0.5 truncate">
                {item.title || `${item.type || "Item"} from ${item.agent}`}
              </p>
            </button>
            <div className="flex items-center gap-1.5 shrink-0">
              <button
                onClick={() => handleOpen(item)}
                className="text-xs px-2.5 py-1 rounded-md border border-[#534AB7]/30 text-[#534AB7] bg-white hover:bg-[#FAFAFF] transition-colors"
              >
                Review
              </button>
              <button
                onClick={() => handleSnooze(item)}
                disabled={snoozing === item.id}
                className="text-xs px-2.5 py-1 rounded-md border border-[#E0DED8] text-[#5F5E5A] bg-white hover:bg-[#F8F8F6] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                title="Hide for 24h"
              >
                {snoozing === item.id ? "Snoozing..." : "Snooze 24h"}
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
