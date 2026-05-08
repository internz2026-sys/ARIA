"use client";

/**
 * Reports tab — persisted "State of the Union" + agent-productivity
 * snapshots generated on-demand from agent_logs / inbox_items /
 * email_messages / campaign_reports.
 *
 * Reads from /api/reports/{tenant_id} (returns rows ordered newest
 * first) and renders each as a ReportCard. The big purple "Generate
 * State of the Union" button kicks a /generate POST that aggregates
 * the last 7 days, asks Haiku to write a 3-paragraph narrative,
 * renders a tasks-per-agent bar chart via the existing visualizer,
 * persists the row, and prepends it to the list.
 *
 * "Quick Snapshot" tile fires the lighter agent_productivity
 * variant — same chart, deterministic blurb, no LLM call. Useful when
 * the user just wants a fresh chart without spending Claude tokens.
 */

import React, { useEffect, useState, useCallback } from "react";
import { reports as reportsApi } from "@/lib/api";
import { useNotifications } from "@/lib/use-notifications";
import { renderMarkdown } from "@/lib/render-markdown";

interface ChartRef {
  url: string;
  type: string;
  title: string;
}

/** Aggregated counters the backend persists alongside each report.
 *  Shape varies by `report_type` — state_of_union has the full union of
 *  fields, agent_productivity is just `tasks_by_agent` + `total`. The
 *  frontend only renders the JSON tree under <details>, so we type it
 *  as a recursive primitive map rather than a strict per-type union. */
type ReportMetricValue =
  | string
  | number
  | boolean
  | null
  | ReportMetricValue[]
  | { [key: string]: ReportMetricValue };

type ReportMetrics = { [key: string]: ReportMetricValue };

interface MarketingReport {
  id: string;
  tenant_id: string;
  report_type: string;
  agent: string | null;
  title: string;
  summary: string | null;
  body_markdown: string | null;
  chart_urls: ChartRef[];
  metrics: ReportMetrics;
  period_start: string | null;
  period_end: string | null;
  created_at: string;
}

const REPORT_TYPE_BADGE: Record<string, { label: string; color: string }> = {
  state_of_union: { label: "State of the Union", color: "bg-[#EEEDFE] text-[#534AB7] border-[#534AB7]/30" },
  agent_productivity: { label: "Agent Productivity", color: "bg-amber-50 text-amber-700 border-amber-200" },
  campaign_roi: { label: "Campaign ROI", color: "bg-emerald-50 text-emerald-700 border-emerald-200" },
  channel_spend: { label: "Channel Spend", color: "bg-blue-50 text-blue-700 border-blue-200" },
  daily_pulse: { label: "Daily Pulse", color: "bg-rose-50 text-rose-700 border-rose-200" },
};

function formatPeriod(report: MarketingReport): string {
  if (!report.period_start || !report.period_end) return "";
  const start = new Date(report.period_start);
  const end = new Date(report.period_end);
  const opts: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" };
  return `${start.toLocaleDateString("en-US", opts)} – ${end.toLocaleDateString("en-US", opts)}`;
}

function relativeTime(iso: string): string {
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export default function ReportsPage() {
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";
  const { showToast } = useNotifications();
  const [reports, setReports] = useState<MarketingReport[]>([]);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState<string | null>(null);
  const [selected, setSelected] = useState<MarketingReport | null>(null);

  const fetchReports = useCallback(async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const res = await reportsApi.list(tenantId, 50);
      setReports(res.reports || []);
    } catch (err: any) {
      showToast({
        title: "Couldn't load reports",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    } finally {
      setLoading(false);
    }
  }, [tenantId, showToast]);

  useEffect(() => {
    fetchReports();
  }, [fetchReports]);

  const handleGenerate = async (reportType: string) => {
    if (!tenantId || generating) return;
    setGenerating(reportType);
    try {
      const res = await reportsApi.generate(tenantId, reportType);
      const created: MarketingReport | null = res?.report || null;
      if (created) {
        setReports((prev) => [created, ...prev]);
        setSelected(created); // open detail modal so the user reads it immediately
        showToast({
          title: "Report generated",
          body: created.title,
          variant: "success",
        });
      }
    } catch (err: any) {
      showToast({
        title: "Couldn't generate report",
        body: err?.message || "Try again in a moment.",
        variant: "error",
      });
    } finally {
      setGenerating(null);
    }
  };

  const handleDelete = async (report: MarketingReport) => {
    if (!tenantId) return;
    try {
      await reportsApi.remove(tenantId, report.id);
      setReports((prev) => prev.filter((r) => r.id !== report.id));
      if (selected?.id === report.id) setSelected(null);
      showToast({ title: "Report deleted", variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't delete report",
        body: err?.message || "Network error.",
        variant: "error",
      });
    }
  };

  return (
    <div className="max-w-screen-2xl mx-auto space-y-6">
      <div>
        <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">Reports</h1>
        <p className="text-sm text-[#5F5E5A] mt-1">
          On-demand snapshots from your marketing agents
        </p>
      </div>

      {/* Generate tiles */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
        <button
          onClick={() => handleGenerate("state_of_union")}
          disabled={!!generating}
          className="bg-white rounded-xl border border-[#E0DED8] p-5 text-left hover:border-[#534AB7]/40 hover:shadow-sm transition disabled:opacity-50 disabled:cursor-wait flex flex-col gap-2"
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wide text-[#534AB7]">Recommended</span>
            {generating === "state_of_union" && (
              <div className="w-3.5 h-3.5 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
            )}
          </div>
          <p className="text-base font-semibold text-[#2C2C2A]">Generate State of the Union</p>
          <p className="text-xs text-[#5F5E5A] leading-relaxed">
            7-day cross-agent narrative from your CEO. Tasks completed, emails sent + received, campaign spend, and a recommended focus for the next week.
          </p>
          <span className="text-[11px] text-[#9E9C95] mt-2">
            {generating === "state_of_union" ? "Aggregating + asking the CEO..." : "Takes ~10–20s · uses Claude tokens"}
          </span>
        </button>

        <button
          onClick={() => handleGenerate("agent_productivity")}
          disabled={!!generating}
          className="bg-white rounded-xl border border-[#E0DED8] p-5 text-left hover:border-[#1D9E75]/40 hover:shadow-sm transition disabled:opacity-50 disabled:cursor-wait flex flex-col gap-2"
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wide text-[#1D9E75]">Quick</span>
            {generating === "agent_productivity" && (
              <div className="w-3.5 h-3.5 border-2 border-[#1D9E75] border-t-transparent rounded-full animate-spin" />
            )}
          </div>
          <p className="text-base font-semibold text-[#2C2C2A]">Agent Productivity Snapshot</p>
          <p className="text-xs text-[#5F5E5A] leading-relaxed">
            Bar chart of tasks completed per agent over 7 days. Same chart as the State of the Union, no LLM narrative — refresh as often as you like.
          </p>
          <span className="text-[11px] text-[#9E9C95] mt-2">
            {generating === "agent_productivity" ? "Rendering chart..." : "Takes ~2–4s · no Claude tokens"}
          </span>
        </button>

        <button
          onClick={() => handleGenerate("campaign_roi")}
          disabled={!!generating}
          className="bg-white rounded-xl border border-[#E0DED8] p-5 text-left hover:border-emerald-400/40 hover:shadow-sm transition disabled:opacity-50 disabled:cursor-wait flex flex-col gap-2"
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wide text-emerald-600">Ad Strategist</span>
            {generating === "campaign_roi" && (
              <div className="w-3.5 h-3.5 border-2 border-emerald-600 border-t-transparent rounded-full animate-spin" />
            )}
          </div>
          <p className="text-base font-semibold text-[#2C2C2A]">Campaign ROI</p>
          <p className="text-xs text-[#5F5E5A] leading-relaxed">
            Funnel chart — Impressions → Clicks → Conversions. Lifetime totals across uploaded Meta Ads reports.
          </p>
          <span className="text-[11px] text-[#9E9C95] mt-2">
            {generating === "campaign_roi" ? "Building funnel chart..." : "Takes ~2–4s · no Claude tokens"}
          </span>
        </button>

        <button
          onClick={() => handleGenerate("channel_spend")}
          disabled={!!generating}
          className="bg-white rounded-xl border border-[#E0DED8] p-5 text-left hover:border-blue-400/40 hover:shadow-sm transition disabled:opacity-50 disabled:cursor-wait flex flex-col gap-2"
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wide text-blue-600">ARIA CEO</span>
            {generating === "channel_spend" && (
              <div className="w-3.5 h-3.5 border-2 border-blue-600 border-t-transparent rounded-full animate-spin" />
            )}
          </div>
          <p className="text-base font-semibold text-[#2C2C2A]">Channel Spend</p>
          <p className="text-xs text-[#5F5E5A] leading-relaxed">
            Pie chart split by channel — Meta Ads, Email, Social. 30-day spend split across paid + owned channels.
          </p>
          <span className="text-[11px] text-[#9E9C95] mt-2">
            {generating === "channel_spend" ? "Calculating spend split..." : "Takes ~2–4s · no Claude tokens"}
          </span>
        </button>

        <button
          onClick={() => handleGenerate("daily_pulse")}
          disabled={!!generating}
          className="bg-white rounded-xl border border-[#E0DED8] p-5 text-left hover:border-rose-400/40 hover:shadow-sm transition disabled:opacity-50 disabled:cursor-wait flex flex-col gap-2"
        >
          <div className="flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-wide text-rose-600">ARIA CEO</span>
            {generating === "daily_pulse" && (
              <div className="w-3.5 h-3.5 border-2 border-rose-600 border-t-transparent rounded-full animate-spin" />
            )}
          </div>
          <p className="text-base font-semibold text-[#2C2C2A]">Daily Pulse</p>
          <p className="text-xs text-[#5F5E5A] leading-relaxed">
            24-hour activity snapshot — tasks done, emails, replies, active campaigns.
          </p>
          <span className="text-[11px] text-[#9E9C95] mt-2">
            {generating === "daily_pulse" ? "Compiling daily snapshot..." : "Takes ~2–4s · no Claude tokens"}
          </span>
        </button>
      </div>

      {/* Reports list */}
      {loading ? (
        <div className="flex justify-center py-12">
          <div className="w-6 h-6 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
        </div>
      ) : reports.length === 0 ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[260px] flex items-center justify-center">
          <div className="text-center px-6 py-12">
            <div className="w-14 h-14 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-3">
              <svg className="w-7 h-7 text-[#9E9C95]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
              </svg>
            </div>
            <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No reports yet</h3>
            <p className="text-sm text-[#5F5E5A] max-w-md mx-auto">
              Click <strong>Generate State of the Union</strong> above to produce your first report from the last 7 days of agent activity.
            </p>
          </div>
        </div>
      ) : (
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
          {reports.map((r) => {
            const badge = REPORT_TYPE_BADGE[r.report_type] || {
              label: r.report_type,
              color: "bg-gray-50 text-gray-700 border-gray-200",
            };
            return (
              <button
                key={r.id}
                onClick={() => setSelected(r)}
                className="text-left bg-white rounded-xl border border-[#E0DED8] p-5 hover:border-[#534AB7]/40 hover:shadow-sm transition flex flex-col gap-2"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className={`px-2 py-0.5 text-[10px] font-semibold rounded-full border ${badge.color}`}>
                    {badge.label}
                  </span>
                  <span className="text-[11px] text-[#9E9C95]">{relativeTime(r.created_at)}</span>
                </div>
                <h3 className="text-base font-semibold text-[#2C2C2A] line-clamp-1">{r.title}</h3>
                {r.summary && (
                  <p className="text-sm text-[#5F5E5A] leading-relaxed line-clamp-3">{r.summary}</p>
                )}
                <div className="flex items-center gap-3 mt-1 text-[11px] text-[#9E9C95]">
                  {r.agent && <span>by {r.agent}</span>}
                  {formatPeriod(r) && <span>• {formatPeriod(r)}</span>}
                  {r.chart_urls.length > 0 && (
                    <span>
                      • {r.chart_urls.length} chart{r.chart_urls.length === 1 ? "" : "s"}
                    </span>
                  )}
                </div>
              </button>
            );
          })}
        </div>
      )}

      {/* Detail modal */}
      {selected && (
        <ReportDetailModal
          report={selected}
          onClose={() => setSelected(null)}
          onDelete={() => handleDelete(selected)}
        />
      )}
    </div>
  );
}


function ReportDetailModal({
  report,
  onClose,
  onDelete,
}: {
  report: MarketingReport;
  onClose: () => void;
  onDelete: () => void;
}) {
  const badge = REPORT_TYPE_BADGE[report.report_type] || {
    label: report.report_type,
    color: "bg-gray-50 text-gray-700 border-gray-200",
  };
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/40" onClick={onClose} />
      <div className="relative bg-white rounded-xl border border-[#E0DED8] shadow-2xl w-full max-w-2xl mx-4 max-h-[90vh] overflow-hidden flex flex-col">
        <div className="flex items-start justify-between px-6 py-4 border-b border-[#E0DED8] shrink-0">
          <div className="min-w-0">
            <span className={`inline-block px-2 py-0.5 text-[10px] font-semibold rounded-full border ${badge.color}`}>
              {badge.label}
            </span>
            <h3 className="text-base font-semibold text-[#2C2C2A] mt-1.5 truncate">{report.title}</h3>
            <p className="text-[11px] text-[#9E9C95] mt-0.5">
              {report.agent && <span>by {report.agent}</span>}
              {formatPeriod(report) && <span> · {formatPeriod(report)}</span>}
              <span> · {relativeTime(report.created_at)}</span>
            </p>
          </div>
          <button onClick={onClose} className="text-[#9E9C95] hover:text-[#2C2C2A] shrink-0 ml-3" aria-label="Close">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {report.summary && (
            <div className="bg-[#F8F8F6] border border-[#E0DED8] rounded-lg p-4">
              <p className="text-sm text-[#2C2C2A] leading-relaxed">{report.summary}</p>
            </div>
          )}

          {report.chart_urls.map((c) => (
            <figure key={c.url} className="bg-white border border-[#E0DED8] rounded-lg overflow-hidden">
              <img src={c.url} alt={c.title} className="w-full h-auto block" />
              {c.title && (
                <figcaption className="px-4 py-2 text-xs text-[#5F5E5A] border-t border-[#E0DED8] bg-[#F8F8F6]">
                  {c.title}
                </figcaption>
              )}
            </figure>
          ))}

          {report.body_markdown && (
            <div className="prose prose-sm max-w-none text-[#2C2C2A] [&_p]:my-3 [&_ul]:my-3 [&_ul]:pl-6 [&_ul]:list-disc [&_ul>li]:my-1 [&_strong]:font-semibold [&_code]:bg-[#F0EFEC] [&_code]:px-1.5 [&_code]:py-0.5 [&_code]:rounded [&_code]:text-[12px]">
              <>{renderMarkdown(report.body_markdown)}</>
            </div>
          )}

          {Object.keys(report.metrics || {}).length > 0 && (
            <details className="text-xs">
              <summary className="cursor-pointer text-[#9E9C95] hover:text-[#2C2C2A]">
                Raw metrics
              </summary>
              <pre className="bg-[#1e1e1e] text-[#d4d4d4] p-3 mt-2 rounded-lg text-[11px] leading-relaxed overflow-x-auto">
                {JSON.stringify(report.metrics, null, 2)}
              </pre>
            </details>
          )}
        </div>

        <div className="border-t border-[#E0DED8] px-6 py-3 flex items-center justify-end gap-2 shrink-0 bg-[#F8F8F6]">
          <button
            onClick={onDelete}
            className="px-3 py-2 text-sm font-medium rounded-lg border border-red-200 text-red-600 hover:bg-red-50 transition"
          >
            Delete
          </button>
          <button
            onClick={onClose}
            className="px-4 py-2 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
