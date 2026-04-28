"use client";

import React, { useState, useEffect, useCallback } from "react";
import { API_URL, authFetch } from "@/lib/api";
import { AGENT_COLORS, AGENT_NAMES } from "@/lib/agent-config";

const dateRanges = [
  { key: "7d", label: "Last 7 days" },
  { key: "30d", label: "Last 30 days" },
  { key: "90d", label: "Last 90 days" },
];

type KpiBucket = {
  content_published: { value: number; delta: number; delta_pct: number };
  emails_sent: { value: number; open_rate: number; click_rate: number };
  social_engagement: { value: number; delta_pct: number };
  ad_spend: { value: number; roas: number };
};

type ActivityDay = { date: string; total?: number; email?: number; social?: number; image?: number; content?: number; ad?: number; other?: number };
type AnalyticsSummary = {
  totals: { items: number; agents_active: number; types_active: number; days_in_range: number };
  activity_series: ActivityDay[];
  by_agent: { agent: string; count: number }[];
  by_type: { type: string; count: number }[];
  by_status: { status: string; count: number }[];
  recent_activity: { id: string; agent: string; type: string; status: string; title: string; created_at: string }[];
  tasks: { total: number; completed: number; in_progress: number; failed: number };
  scheduled_tasks: { upcoming: number; executed: number; failed: number };
};

const TYPE_COLORS: Record<string, string> = {
  email: "#BA7517",
  social: "#D85A30",
  image: "#E4407B",
  content: "#1D9E75",
  ad: "#7C3AED",
  other: "#9E9C95",
};

const STATUS_COLORS: Record<string, string> = {
  completed: "#1D9E75",
  sent: "#1D9E75",
  published: "#1D9E75",
  ready: "#3B82F6",
  needs_review: "#F59E0B",
  draft_pending_approval: "#BA7517",
  in_progress: "#3B82F6",
  processing: "#3B82F6",
  failed: "#EF4444",
  cancelled: "#9E9C95",
  canceled: "#9E9C95",
};

function formatRelative(iso: string): string {
  const ts = Date.parse(iso || "");
  if (!ts) return "";
  const diff = Math.max(0, Date.now() - ts);
  const m = Math.floor(diff / 60_000);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function formatDayLabel(iso: string): string {
  try {
    const d = new Date(iso + "T00:00:00Z");
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric" });
  } catch {
    return iso;
  }
}

export default function AnalyticsPage() {
  const [activeRange, setActiveRange] = useState("7d");
  const [summary, setSummary] = useState<AnalyticsSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [kpis, setKpis] = useState<KpiBucket>({
    content_published: { value: 0, delta: 0, delta_pct: 0 },
    emails_sent: { value: 0, open_rate: 0, click_rate: 0 },
    social_engagement: { value: 0, delta_pct: 0 },
    ad_spend: { value: 0, roas: 0 },
  });
  const [enabledAgents, setEnabledAgents] = useState<string[]>([]);

  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchSummary = useCallback(async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const res = await authFetch(`${API_URL}/api/analytics/${tenantId}?date_range=${activeRange}`);
      if (res.ok) {
        const data = await res.json();
        setSummary(data);
      }
    } catch {
      // Silent fail — UI shows the empty state below
    } finally {
      setLoading(false);
    }
  }, [tenantId, activeRange]);

  useEffect(() => {
    fetchSummary();
  }, [fetchSummary]);

  useEffect(() => {
    if (!tenantId) return;
    authFetch(`${API_URL}/api/dashboard/${tenantId}/stats`)
      .then(r => r.json())
      .then(d => d.kpis && setKpis(d.kpis))
      .catch(() => {});
    authFetch(`${API_URL}/api/dashboard/${tenantId}/config`)
      .then(r => r.json())
      .then(d => Array.isArray(d.active_agents) && setEnabledAgents(d.active_agents))
      .catch(() => {});
  }, [tenantId]);

  const allKpiCards = [
    {
      label: "Content Published",
      value: kpis.content_published.value,
      display: String(kpis.content_published.value),
      sub: kpis.content_published.delta ? `+${kpis.content_published.delta} this week` : "Drafts will appear in your inbox",
      requiresAgent: "content_writer",
      accent: "#1D9E75",
    },
    {
      label: "Emails Sent",
      value: kpis.emails_sent.value,
      display: kpis.emails_sent.value.toLocaleString(),
      sub: kpis.emails_sent.value ? `${kpis.emails_sent.open_rate}% open rate` : "Not sending yet",
      requiresAgent: "email_marketer",
      accent: "#BA7517",
    },
    {
      label: "Social Engagement",
      value: kpis.social_engagement.value,
      display: String(kpis.social_engagement.value),
      sub: kpis.social_engagement.value ? `+${kpis.social_engagement.delta_pct}% vs last week` : "No posts yet",
      requiresAgent: "social_manager",
      accent: "#D85A30",
    },
    {
      label: "Ad Spend",
      value: kpis.ad_spend.value,
      display: kpis.ad_spend.value ? `$${kpis.ad_spend.value}` : "$0",
      sub: kpis.ad_spend.value ? `${kpis.ad_spend.roas}x ROAS` : "No campaigns running",
      requiresAgent: "ad_strategist",
      accent: "#7C3AED",
    },
  ];
  const kpiCards = allKpiCards.filter(k => k.value > 0 || enabledAgents.includes(k.requiresAgent));

  const hasAnyActivity = (summary?.totals?.items || 0) > 0;
  const maxDay = Math.max(1, ...(summary?.activity_series || []).map(d => d.total || 0));
  const totalAgentWork = Math.max(1, ...(summary?.by_agent || []).map(a => a.count));
  const totalByType = (summary?.by_type || []).reduce((s, t) => s + t.count, 0) || 1;
  const totalByStatus = (summary?.by_status || []).reduce((s, t) => s + t.count, 0) || 1;

  return (
    <div className="max-w-[1400px] space-y-6">
      {/* Header — sticky on mobile so the date-range pills stay reachable
          while scrolling through the long stat list. lg+ stays inline. */}
      <div className="sticky top-14 lg:top-0 z-30 -mx-6 px-6 lg:mx-0 lg:px-0 py-2 lg:py-0 bg-[#F8F8F6]/85 backdrop-blur-md lg:bg-transparent lg:backdrop-blur-0 flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold text-[#2C2C2A]">Analytics</h1>
          <p className="text-sm text-[#5F5E5A] mt-0.5">Performance, output, and agent activity at a glance</p>
        </div>
        <div className="flex items-center gap-1 bg-white rounded-lg border border-[#E0DED8] p-1 self-start sm:self-auto overflow-x-auto">
          {dateRanges.map((r) => (
            <button
              key={r.key}
              onClick={() => setActiveRange(r.key)}
              className={`px-3 py-1.5 rounded-md text-xs font-medium whitespace-nowrap transition-colors ${
                activeRange === r.key ? "bg-[#534AB7] text-white" : "text-[#5F5E5A] hover:bg-[#F8F8F6]"
              }`}
            >
              {r.label}
            </button>
          ))}
        </div>
      </div>

      {/* KPI cards — business outcomes */}
      {kpiCards.length > 0 && (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {kpiCards.map((kpi) => (
            <div
              key={kpi.label}
              className="bg-white rounded-xl border border-[#E0DED8] p-5 hover:shadow-sm transition-shadow relative overflow-hidden"
            >
              <div
                className="absolute top-0 left-0 w-1 h-full"
                style={{ backgroundColor: kpi.accent }}
              />
              <p className="text-sm text-[#5F5E5A] font-medium">{kpi.label}</p>
              <p className={`text-3xl font-semibold mt-1 ${kpi.value ? "text-[#2C2C2A]" : "text-[#E0DED8]"}`}>{kpi.display}</p>
              <p className={`text-xs mt-2 ${kpi.value ? "text-[#1D9E75] font-medium" : "text-[#6B6A65]"}`}>{kpi.sub}</p>
            </div>
          ))}
        </div>
      )}

      {/* Operational totals strip */}
      {summary && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
          <div className="bg-white rounded-lg border border-[#E0DED8] p-4">
            <p className="text-[11px] font-semibold text-[#5F5E5A] uppercase tracking-wide">Total items</p>
            <p className="text-xl font-bold text-[#2C2C2A] mt-1">{summary.totals.items.toLocaleString()}</p>
            <p className="text-[11px] text-[#9E9C95] mt-0.5">in last {summary.totals.days_in_range} days</p>
          </div>
          <div className="bg-white rounded-lg border border-[#E0DED8] p-4">
            <p className="text-[11px] font-semibold text-[#5F5E5A] uppercase tracking-wide">Tasks completed</p>
            <p className="text-xl font-bold text-[#1D9E75] mt-1">{summary.tasks.completed}</p>
            <p className="text-[11px] text-[#9E9C95] mt-0.5">
              {summary.tasks.in_progress} in progress · {summary.tasks.failed} failed
            </p>
          </div>
          <div className="bg-white rounded-lg border border-[#E0DED8] p-4">
            <p className="text-[11px] font-semibold text-[#5F5E5A] uppercase tracking-wide">Scheduled upcoming</p>
            <p className="text-xl font-bold text-[#534AB7] mt-1">{summary.scheduled_tasks.upcoming}</p>
            <p className="text-[11px] text-[#9E9C95] mt-0.5">
              {summary.scheduled_tasks.executed} executed · {summary.scheduled_tasks.failed} failed
            </p>
          </div>
          <div className="bg-white rounded-lg border border-[#E0DED8] p-4">
            <p className="text-[11px] font-semibold text-[#5F5E5A] uppercase tracking-wide">Agents active</p>
            <p className="text-xl font-bold text-[#2C2C2A] mt-1">{summary.totals.agents_active}</p>
            <p className="text-[11px] text-[#9E9C95] mt-0.5">{summary.totals.types_active} content types</p>
          </div>
        </div>
      )}

      {loading ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[300px] flex items-center justify-center">
          <div className="animate-pulse text-sm text-[#9E9C95]">Loading analytics…</div>
        </div>
      ) : !hasAnyActivity ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[300px] flex items-center justify-center">
          <div className="text-center px-6 py-16">
            <div className="w-16 h-16 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-4">
              <svg className="w-8 h-8 text-[#E0DED8]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
              </svg>
            </div>
            <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No activity in this range yet</h3>
            <p className="text-sm text-[#5F5E5A] max-w-sm mx-auto">
              Charts and breakdowns populate as your agents create content, send emails, publish posts, or run campaigns.
            </p>
          </div>
        </div>
      ) : (
        <>
          {/* Activity over time — stacked bars per day */}
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-base font-semibold text-[#2C2C2A]">Activity over time</h2>
                <p className="text-xs text-[#5F5E5A] mt-0.5">Items produced per day, broken down by content type</p>
              </div>
              <div className="flex items-center gap-3 flex-wrap">
                {Object.entries(TYPE_COLORS).map(([k, color]) => (
                  <div key={k} className="flex items-center gap-1.5">
                    <span className="w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: color }} />
                    <span className="text-[11px] text-[#5F5E5A] capitalize">{k}</span>
                  </div>
                ))}
              </div>
            </div>
            <div className="flex items-end gap-1.5 h-48 overflow-x-auto -mx-2 px-2 sm:mx-0 sm:px-0">
              {summary!.activity_series.map((d) => {
                const total = d.total || 0;
                const heightPct = (total / maxDay) * 100;
                return (
                  <div key={d.date} className="flex-1 min-w-[28px] sm:min-w-[22px] flex flex-col items-center gap-1 h-full justify-end group">
                    <div className="w-full flex flex-col-reverse h-full justify-end" style={{ height: `${heightPct}%` }} title={`${d.date}: ${total} items`}>
                      {Object.keys(TYPE_COLORS).map((key) => {
                        const v = (d as any)[key] as number | undefined;
                        if (!v) return null;
                        return (
                          <div
                            key={key}
                            className="w-full transition-opacity group-hover:opacity-80"
                            style={{ backgroundColor: TYPE_COLORS[key], height: `${(v / total) * 100}%`, minHeight: 2 }}
                          />
                        );
                      })}
                    </div>
                    <span className="text-[9px] text-[#9E9C95] whitespace-nowrap">{formatDayLabel(d.date).replace(", ", " ")}</span>
                  </div>
                );
              })}
            </div>
          </div>

          {/* Two-column: by-agent + by-type */}
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
              <h2 className="text-base font-semibold text-[#2C2C2A] mb-4">By agent</h2>
              <div className="space-y-3">
                {summary!.by_agent.map((a) => {
                  const color = AGENT_COLORS[a.agent] || "#5F5E5A";
                  const pct = (a.count / totalAgentWork) * 100;
                  return (
                    <div key={a.agent} className="flex items-center gap-3">
                      <div className="w-28 flex items-center gap-2">
                        <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: color }} />
                        <span className="text-xs font-medium text-[#2C2C2A] truncate">
                          {AGENT_NAMES[a.agent] || a.agent}
                        </span>
                      </div>
                      <div className="flex-1 h-2.5 bg-[#F8F8F6] rounded-full overflow-hidden">
                        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: color }} />
                      </div>
                      <span className="w-10 text-right text-xs font-semibold text-[#2C2C2A]">{a.count}</span>
                    </div>
                  );
                })}
              </div>
            </div>

            <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
              <h2 className="text-base font-semibold text-[#2C2C2A] mb-4">By content type</h2>
              <div className="space-y-3">
                {summary!.by_type.map((t) => {
                  const bucket: string = TYPE_COLORS[(t.type || "").split("_")[0]] ? (t.type || "").split("_")[0] : "other";
                  const color = TYPE_COLORS[bucket] || "#5F5E5A";
                  const pct = (t.count / totalByType) * 100;
                  return (
                    <div key={t.type} className="flex items-center gap-3">
                      <div className="w-28 flex items-center gap-2">
                        <span className="w-2.5 h-2.5 rounded-sm shrink-0" style={{ backgroundColor: color }} />
                        <span className="text-xs font-medium text-[#2C2C2A] truncate capitalize">{t.type.replace(/_/g, " ")}</span>
                      </div>
                      <div className="flex-1 h-2.5 bg-[#F8F8F6] rounded-full overflow-hidden">
                        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: color }} />
                      </div>
                      <span className="w-12 text-right text-xs font-semibold text-[#2C2C2A]">
                        {Math.round(pct)}%
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* Status distribution — single stacked bar */}
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-base font-semibold text-[#2C2C2A]">Status distribution</h2>
              <span className="text-xs text-[#9E9C95]">{totalByStatus} items</span>
            </div>
            <div className="flex h-8 rounded-lg overflow-hidden">
              {summary!.by_status.map((s) => {
                const color = STATUS_COLORS[s.status] || "#9E9C95";
                const pct = (s.count / totalByStatus) * 100;
                return (
                  <div
                    key={s.status}
                    className="h-full transition-opacity hover:opacity-85"
                    style={{ width: `${pct}%`, backgroundColor: color }}
                    title={`${s.status}: ${s.count} (${Math.round(pct)}%)`}
                  />
                );
              })}
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-2 mt-3">
              {summary!.by_status.map((s) => (
                <div key={s.status} className="flex items-center gap-1.5">
                  <span
                    className="w-2.5 h-2.5 rounded-sm"
                    style={{ backgroundColor: STATUS_COLORS[s.status] || "#9E9C95" }}
                  />
                  <span className="text-xs text-[#5F5E5A] capitalize">{s.status.replace(/_/g, " ")}</span>
                  <span className="text-xs font-semibold text-[#2C2C2A]">{s.count}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Recent activity feed */}
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-4">Recent activity</h2>
            <div className="divide-y divide-[#E0DED8]">
              {summary!.recent_activity.map((item) => (
                <div key={item.id} className="flex items-start gap-3 py-3">
                  <span
                    className="mt-1.5 w-2.5 h-2.5 rounded-full shrink-0"
                    style={{ backgroundColor: AGENT_COLORS[item.agent] || "#9E9C95" }}
                  />
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-[#2C2C2A] truncate">{item.title || "(untitled)"}</p>
                    <div className="flex items-center gap-2 mt-0.5 text-[11px] text-[#5F5E5A]">
                      <span className="font-medium" style={{ color: AGENT_COLORS[item.agent] || "#5F5E5A" }}>
                        {AGENT_NAMES[item.agent] || item.agent}
                      </span>
                      <span>·</span>
                      <span className="capitalize">{(item.type || "").replace(/_/g, " ")}</span>
                      <span>·</span>
                      <span
                        className="px-1.5 py-0.5 rounded"
                        style={{
                          backgroundColor: (STATUS_COLORS[item.status] || "#9E9C95") + "20",
                          color: STATUS_COLORS[item.status] || "#9E9C95",
                        }}
                      >
                        {(item.status || "").replace(/_/g, " ")}
                      </span>
                    </div>
                  </div>
                  <span className="text-[11px] text-[#9E9C95] shrink-0">{formatRelative(item.created_at)}</span>
                </div>
              ))}
              {summary!.recent_activity.length === 0 && (
                <p className="text-sm text-[#9E9C95] py-3">No recent activity in this range.</p>
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
