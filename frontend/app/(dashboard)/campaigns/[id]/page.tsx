"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import { useParams, useRouter } from "next/navigation";
import { campaigns as campaignsApi } from "@/lib/api";
import CampaignCopyPasteTab from "@/components/shared/CampaignCopyPasteTab";

/* ─── Helpers ─── */

function fmt(v: number | null | undefined, prefix = "") {
  if (v == null) return "—";
  return `${prefix}${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
function fmtInt(v: number | null | undefined) {
  if (v == null) return "—";
  return v.toLocaleString("en-US");
}
function fmtDate(d: string | null | undefined) {
  if (!d) return "—";
  return new Date(d).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}
function fmtDateTime(d: string | null | undefined) {
  if (!d) return "—";
  return new Date(d).toLocaleString("en-US", { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" });
}

/* ─── Metric Card ─── */

function MetricCard({ label, value, prefix }: { label: string; value: number | null | undefined; prefix?: string }) {
  return (
    <div className="bg-[#F8F8F6] rounded-lg p-3">
      <p className="text-[10px] text-[#9E9C95] uppercase font-medium">{label}</p>
      <p className="text-lg font-bold text-[#2C2C2A] mt-0.5">
        {value != null ? (prefix === "$" ? fmt(value, "$") : prefix === "%" ? `${value}%` : fmtInt(value)) : "—"}
      </p>
    </div>
  );
}

/* ─── Markdown Renderer (simple) ─── */

/** Render inline markdown: **bold**, *italic*, `code` */
function renderInline(text: string): React.ReactNode[] {
  const parts: React.ReactNode[] = [];
  const regex = /(\*\*(.+?)\*\*)|(\*(.+?)\*)|(`(.+?)`)/g;
  let lastIndex = 0;
  let match;
  let key = 0;
  while ((match = regex.exec(text)) !== null) {
    if (match.index > lastIndex) parts.push(text.slice(lastIndex, match.index));
    if (match[2]) parts.push(<strong key={key++} className="font-semibold">{match[2]}</strong>);
    else if (match[4]) parts.push(<em key={key++}>{match[4]}</em>);
    else if (match[6]) parts.push(<code key={key++} className="bg-[#F0EFEC] px-1 py-0.5 rounded text-xs font-mono">{match[6]}</code>);
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) parts.push(text.slice(lastIndex));
  return parts;
}

/** Parse a markdown table block (array of lines) into a <table> */
function renderTable(lines: string[], startKey: number): React.ReactNode {
  // lines[0] = header, lines[1] = separator, lines[2..] = data rows
  const parseRow = (line: string) =>
    line.split("|").map((c) => c.trim()).filter((c) => c.length > 0);

  const headers = parseRow(lines[0]);
  const rows = lines.slice(2).map(parseRow);

  return (
    <div key={startKey} className="overflow-x-auto my-3">
      <table className="w-full text-sm border-collapse">
        <thead>
          <tr className="border-b-2 border-[#E0DED8]">
            {headers.map((h, i) => (
              <th key={i} className="text-left py-2 px-3 font-semibold text-[#2C2C2A] bg-[#F8F8F6]">
                {renderInline(h)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri} className={`border-b border-[#E0DED8] ${ri % 2 === 0 ? "" : "bg-[#FAFAF8]"}`}>
              {row.map((cell, ci) => (
                <td key={ci} className="py-2 px-3 text-[#2C2C2A]">
                  {renderInline(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Markdown({ text }: { text: string }) {
  if (!text) return <p className="text-sm text-[#9E9C95] italic">No AI analysis yet.</p>;

  const lines = text.split("\n");
  const elements: React.ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Table detection: line with | and next line is separator (|---|)
    if (line.includes("|") && i + 1 < lines.length && /^\|?[\s-:|]+\|/.test(lines[i + 1])) {
      const tableLines: string[] = [line, lines[i + 1]];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim() !== "") {
        tableLines.push(lines[i]);
        i++;
      }
      elements.push(renderTable(tableLines, i));
      continue;
    }

    // Horizontal rule
    if (/^---+$/.test(line.trim())) {
      elements.push(<hr key={i} className="my-4 border-[#E0DED8]" />);
      i++;
      continue;
    }

    // Headers
    if (line.startsWith("# ")) { elements.push(<h2 key={i} className="text-lg font-bold mt-5 mb-2 text-[#2C2C2A]">{renderInline(line.slice(2))}</h2>); i++; continue; }
    if (line.startsWith("## ")) { elements.push(<h3 key={i} className="text-base font-semibold mt-4 mb-2 text-[#2C2C2A]">{renderInline(line.slice(3))}</h3>); i++; continue; }
    if (line.startsWith("### ")) { elements.push(<h4 key={i} className="text-sm font-semibold mt-3 mb-1 text-[#534AB7]">{renderInline(line.slice(4))}</h4>); i++; continue; }

    // Bullet list
    if (line.startsWith("- ") || line.startsWith("* ")) {
      elements.push(<li key={i} className="text-sm ml-5 list-disc leading-relaxed">{renderInline(line.slice(2))}</li>);
      i++;
      continue;
    }

    // Numbered list
    if (/^\d+\.\s/.test(line)) {
      elements.push(<li key={i} className="text-sm ml-5 list-decimal leading-relaxed">{renderInline(line.replace(/^\d+\.\s/, ""))}</li>);
      i++;
      continue;
    }

    // Empty line
    if (!line.trim()) { elements.push(<div key={i} className="h-2" />); i++; continue; }

    // Regular paragraph with inline formatting
    elements.push(<p key={i} className="text-sm leading-relaxed">{renderInline(line)}</p>);
    i++;
  }

  return <div className="max-w-none text-[#2C2C2A] space-y-1">{elements}</div>;
}

/* ─── Upload Button ─── */

function UploadReportButton({ tenantId, campaignId, onSuccess }: { tenantId: string; campaignId: string; onSuccess: () => void }) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  const handleFile = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    setLoading(true);
    setError("");
    try {
      await campaignsApi.upload(tenantId, file, campaignId);
      onSuccess();
    } catch (err: any) {
      setError(err.message || "Upload failed");
    } finally {
      setLoading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  return (
    <div>
      <input ref={fileRef} type="file" accept=".csv" className="hidden" onChange={handleFile} />
      <button
        onClick={() => fileRef.current?.click()}
        disabled={loading}
        className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] hover:bg-[#F8F8F6] transition disabled:opacity-40"
      >
        {loading ? (
          <><div className="w-3.5 h-3.5 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" /> Uploading...</>
        ) : (
          <><svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" /></svg> Upload New Report</>
        )}
      </button>
      {error && <p className="text-xs text-red-600 mt-1">{error}</p>}
    </div>
  );
}

/* ─── Main Page ─── */

export default function CampaignDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";
  const [campaign, setCampaign] = useState<any>(null);
  const [reports, setReports] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [aiLoading, setAiLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<"overview" | "copypaste" | "ai" | "history">("overview");

  const load = useCallback(async () => {
    if (!tenantId || !id) return;
    setLoading(true);
    try {
      const [camp, reps] = await Promise.all([
        campaignsApi.get(tenantId, id),
        campaignsApi.listReports(tenantId, id),
      ]);
      setCampaign(camp);
      setReports(reps.reports || []);
    } catch (e) {
      console.error("Failed to load campaign", e);
    } finally {
      setLoading(false);
    }
  }, [tenantId, id]);

  useEffect(() => { load(); }, [load]);

  // Auto-refresh while AI report is generating (polls every 3s)
  useEffect(() => {
    const latestReport = reports[0];
    if (!latestReport) return;
    if (latestReport.ai_summary_status !== "generating" && latestReport.ai_summary_status !== "pending") return;
    const interval = setInterval(async () => {
      try {
        const reps = await campaignsApi.listReports(tenantId, id);
        const updated = reps.reports || [];
        setReports(updated);
        // Also refresh campaign to get latest_report
        const camp = await campaignsApi.get(tenantId, id);
        setCampaign(camp);
        // Stop polling if AI is done
        if (updated[0]?.ai_summary_status === "completed" || updated[0]?.ai_summary_status === "failed") {
          clearInterval(interval);
        }
      } catch {}
    }, 3000);
    return () => clearInterval(interval);
  }, [reports[0]?.ai_summary_status, tenantId, id]);

  const downloadReportPdf = (reportText: string, recommendations: string | null, campaignName: string) => {
    const win = window.open("", "_blank");
    if (!win) return;

    // Convert markdown to simple HTML for print
    const toHtml = (md: string) =>
      md
        // Tables: detect header + separator + rows
        .replace(/^(\|.+\|)\n(\|[-| :]+\|)\n((?:\|.+\|\n?)+)/gm, (_m, header: string, _sep: string, body: string) => {
          const parseRow = (r: string) => r.split("|").filter(c => c.trim()).map(c => c.trim());
          const heads = parseRow(header).map(h => `<th style="border:1px solid #ddd;padding:8px 12px;background:#f5f5f5;text-align:left;font-weight:600">${h}</th>`).join("");
          const rows = body.trim().split("\n").map(r => {
            const cells = parseRow(r).map(c => `<td style="border:1px solid #ddd;padding:8px 12px">${c}</td>`).join("");
            return `<tr>${cells}</tr>`;
          }).join("");
          return `<table style="border-collapse:collapse;width:100%;margin:16px 0"><thead><tr>${heads}</tr></thead><tbody>${rows}</tbody></table>`;
        })
        .replace(/^### (.+)$/gm, '<h3 style="color:#534AB7;margin:16px 0 8px">$1</h3>')
        .replace(/^## (.+)$/gm, '<h2 style="margin:20px 0 10px">$1</h2>')
        .replace(/^# (.+)$/gm, '<h1 style="margin:24px 0 12px">$1</h1>')
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/\*(.+?)\*/g, "<em>$1</em>")
        .replace(/^- (.+)$/gm, '<li style="margin:4px 0 4px 20px">$1</li>')
        .replace(/^---+$/gm, '<hr style="border:none;border-top:1px solid #ddd;margin:20px 0">')
        .replace(/\n/g, "<br>");

    win.document.write(`<!DOCTYPE html><html><head><title>${campaignName} — AI Report</title>
      <style>body{font-family:-apple-system,system-ui,sans-serif;max-width:800px;margin:40px auto;padding:0 20px;color:#2C2C2A;font-size:14px;line-height:1.6}
      h1{font-size:22px}h2{font-size:16px}h3{font-size:14px}
      @media print{body{margin:20px}}</style></head><body>
      <h1>${campaignName} — AI Campaign Report</h1>
      <p style="color:#999;font-size:12px">Generated by Ad Strategist — ${new Date().toLocaleDateString()}</p>
      <hr style="border:none;border-top:1px solid #ddd;margin:20px 0">
      ${toHtml(reportText)}
      ${recommendations ? `<h2 style="margin-top:30px">Recommendations</h2>${toHtml(recommendations)}` : ""}
      </body></html>`);
    win.document.close();
    setTimeout(() => { win.print(); }, 500);
  };

  const generateAiReport = async () => {
    const latestReport = reports[0];
    if (!latestReport) return;
    setAiLoading(true);
    try {
      const result = await campaignsApi.generateAiReport(tenantId, latestReport.id);
      // Reload to get updated report
      await load();
      setActiveTab("ai");
    } catch (e: any) {
      alert(e.message || "AI analysis failed");
    } finally {
      setAiLoading(false);
    }
  };

  if (loading) {
    return (
      <div className="flex justify-center items-center h-64">
        <div className="w-6 h-6 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (!campaign) {
    return (
      <div className="p-8 text-center">
        <p className="text-[#5F5E5A]">Campaign not found.</p>
        <button onClick={() => router.push("/campaigns")} className="mt-4 text-sm text-[#534AB7] underline">Back to campaigns</button>
      </div>
    );
  }

  const latestReport = campaign.latest_report || reports[0];
  const metrics = latestReport?.raw_metrics_json?.totals || latestReport?.raw_metrics_json?.campaigns?.[0]?.metrics || {};

  const STATUS_COLORS: Record<string, string> = {
    active: "bg-green-50 text-green-700",
    paused: "bg-yellow-50 text-yellow-700",
    completed: "bg-blue-50 text-blue-700",
    draft: "bg-gray-50 text-gray-600",
  };

  return (
    <div className="p-6 md:p-8 max-w-5xl mx-auto space-y-6">
      {/* Back + Header */}
      <div>
        <button onClick={() => router.push("/campaigns")} className="text-xs text-[#9E9C95] hover:text-[#2C2C2A] mb-3 flex items-center gap-1">
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" /></svg>
          Back to Campaigns
        </button>
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-xl font-bold text-[#2C2C2A]">{campaign.campaign_name}</h1>
              <span className={`px-2 py-0.5 text-[10px] font-semibold rounded-full uppercase ${STATUS_COLORS[campaign.status] || STATUS_COLORS.draft}`}>
                {campaign.status}
              </span>
            </div>
            <div className="flex items-center gap-3 text-xs text-[#9E9C95] mt-1">
              <span className="capitalize">{campaign.platform}</span>
              {campaign.objective && <><span>•</span><span>{campaign.objective}</span></>}
              {campaign.budget && <><span>•</span><span>Budget: {fmt(campaign.budget, "$")}</span></>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <UploadReportButton tenantId={tenantId} campaignId={id} onSuccess={load} />
            {latestReport && latestReport.ai_summary_status !== "completed" && (
              <button
                onClick={generateAiReport}
                disabled={aiLoading}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition disabled:opacity-40"
              >
                {aiLoading ? (
                  <><div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" /> Analyzing...</>
                ) : (
                  <><svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" /></svg> Generate AI Report</>
                )}
              </button>
            )}
          </div>
        </div>
      </div>

      {/* Data Freshness Notice */}
      {latestReport && (
        <div className="bg-blue-50 border border-blue-100 rounded-lg px-4 py-2.5 flex items-center gap-2">
          <svg className="w-4 h-4 text-blue-500 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M11.25 11.25l.041-.02a.75.75 0 011.063.852l-.708 2.836a.75.75 0 001.063.853l.041-.021M21 12a9 9 0 11-18 0 9 9 0 0118 0zm-9-3.75h.008v.008H12V8.25z" /></svg>
          <p className="text-xs text-blue-700">
            Data from uploaded report: <strong>{latestReport.source_file_name}</strong> — uploaded {fmtDateTime(latestReport.uploaded_at)}
          </p>
        </div>
      )}

      {/* Key Metrics */}
      {latestReport && Object.keys(metrics).length > 0 && (
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
          <MetricCard label="Spend" value={metrics.spend} prefix="$" />
          <MetricCard label="Impressions" value={metrics.impressions} />
          <MetricCard label="Clicks" value={metrics.clicks || metrics.link_clicks} />
          <MetricCard label="CTR" value={metrics.ctr} prefix="%" />
          <MetricCard label="CPC" value={metrics.cpc} prefix="$" />
          <MetricCard label="Conversions" value={metrics.conversions} />
        </div>
      )}

      {/* Tabs */}
      <div className="border-b border-[#E0DED8]">
        <div className="flex gap-6 overflow-x-auto">
          {(() => {
            const tabs: Array<{ key: typeof activeTab; label: string }> = [
              { key: "overview", label: "Overview" },
            ];
            // Copy-Paste tab only when this campaign was created from an Ad
            // Strategist inbox draft (campaigns mirror sets inbox_item_id).
            if (campaign?.inbox_item_id) {
              tabs.push({ key: "copypaste", label: "Copy-Paste" });
            }
            tabs.push({ key: "ai", label: "AI Report" });
            tabs.push({ key: "history", label: "Report History" });
            return tabs.map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setActiveTab(key)}
                className={`pb-2 text-sm font-medium border-b-2 transition whitespace-nowrap ${
                  activeTab === key
                    ? "border-[#534AB7] text-[#534AB7]"
                    : "border-transparent text-[#9E9C95] hover:text-[#2C2C2A]"
                }`}
              >
                {label}
              </button>
            ));
          })()}
        </div>
      </div>

      {/* Tab Content */}
      {activeTab === "overview" && (
        <div className="space-y-6">
          {/* Campaign Info */}
          <div className="bg-white rounded-xl border border-[#E0DED8] p-5 space-y-3">
            <h3 className="text-sm font-semibold text-[#2C2C2A]">Campaign Details</h3>
            <div className="grid grid-cols-2 gap-4 text-sm">
              <div><span className="text-[#9E9C95]">Platform:</span> <span className="ml-2 text-[#2C2C2A] capitalize">{campaign.platform}</span></div>
              <div><span className="text-[#9E9C95]">Status:</span> <span className="ml-2 text-[#2C2C2A] capitalize">{campaign.status}</span></div>
              <div><span className="text-[#9E9C95]">Objective:</span> <span className="ml-2 text-[#2C2C2A]">{campaign.objective || "—"}</span></div>
              <div><span className="text-[#9E9C95]">Budget:</span> <span className="ml-2 text-[#2C2C2A]">{campaign.budget ? fmt(campaign.budget, "$") : "—"}</span></div>
              <div><span className="text-[#9E9C95]">Date Range:</span> <span className="ml-2 text-[#2C2C2A]">{fmtDate(campaign.date_range_start)} – {fmtDate(campaign.date_range_end)}</span></div>
              <div><span className="text-[#9E9C95]">Source:</span> <span className="ml-2 text-[#2C2C2A]">Manual upload (CSV)</span></div>
            </div>
            {campaign.notes && (
              <div className="pt-2 border-t border-[#E0DED8]">
                <p className="text-xs text-[#9E9C95] mb-1">Notes</p>
                <p className="text-sm text-[#2C2C2A]">{campaign.notes}</p>
              </div>
            )}
          </div>

          {/* Extended Metrics */}
          {latestReport && Object.keys(metrics).length > 0 && (
            <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
              <h3 className="text-sm font-semibold text-[#2C2C2A] mb-3">All Metrics (Latest Report)</h3>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-3">
                {Object.entries(metrics).map(([key, val]) => (
                  <MetricCard
                    key={key}
                    label={key.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase())}
                    value={val as number}
                    prefix={["spend", "cpc", "cpm", "cost_per_result"].includes(key) ? "$" : key === "ctr" ? "%" : undefined}
                  />
                ))}
              </div>
            </div>
          )}

          {!latestReport && (
            <div className="bg-white rounded-xl border border-[#E0DED8] p-8 text-center">
              <p className="text-sm text-[#5F5E5A] mb-3">No reports uploaded yet. Upload a Facebook Ads CSV to see metrics.</p>
              <UploadReportButton tenantId={tenantId} campaignId={id} onSuccess={load} />
            </div>
          )}
        </div>
      )}

      {activeTab === "copypaste" && (
        <CampaignCopyPasteTab
          tenantId={tenantId}
          campaign={campaign}
          onCampaignUpdate={load}
        />
      )}

      {activeTab === "ai" && (
        <div className="space-y-6">
          {latestReport?.ai_summary_status === "completed" ? (
            <>
              <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-sm font-semibold text-[#2C2C2A]">AI Campaign Analysis</h3>
                  <div className="flex items-center gap-3">
                    <span className="text-[10px] text-[#9E9C95]">Generated by Ad Strategist</span>
                    <button
                      onClick={() => downloadReportPdf(latestReport.ai_report_text, latestReport.ai_recommendations, campaign.name)}
                      className="flex items-center gap-1.5 px-2.5 py-1 text-xs font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition"
                    >
                      <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
                      </svg>
                      Download PDF
                    </button>
                  </div>
                </div>
                <Markdown text={latestReport.ai_report_text} />
              </div>
              {latestReport.ai_recommendations && (
                <div className="bg-gradient-to-r from-[#EEEDFE] to-[#F8F8F6] rounded-xl border border-[#534AB7]/15 p-5">
                  <h3 className="text-sm font-semibold text-[#2C2C2A] mb-2">Recommendations</h3>
                  <Markdown text={latestReport.ai_recommendations} />
                </div>
              )}
            </>
          ) : latestReport?.ai_summary_status === "generating" || aiLoading ? (
            <div className="bg-white rounded-xl border border-[#E0DED8] p-8 text-center">
              <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin mx-auto mb-3" />
              <p className="text-sm text-[#5F5E5A]">Ad Strategist is analyzing your campaign data...</p>
            </div>
          ) : latestReport ? (
            <div className="bg-white rounded-xl border border-[#E0DED8] p-8 text-center">
              <p className="text-sm text-[#5F5E5A] mb-3">No AI analysis generated yet for this report.</p>
              <button
                onClick={generateAiReport}
                className="px-4 py-2 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition"
              >
                Generate AI Report
              </button>
            </div>
          ) : (
            <div className="bg-white rounded-xl border border-[#E0DED8] p-8 text-center">
              <p className="text-sm text-[#5F5E5A]">Upload a report first to generate an AI analysis.</p>
            </div>
          )}
        </div>
      )}

      {activeTab === "history" && (
        <div className="space-y-3">
          {reports.length === 0 ? (
            <div className="bg-white rounded-xl border border-[#E0DED8] p-8 text-center">
              <p className="text-sm text-[#5F5E5A]">No reports uploaded yet.</p>
            </div>
          ) : (
            reports.map((r: any, i: number) => (
              <div key={r.id} className={`bg-white rounded-xl border p-4 ${i === 0 ? "border-[#534AB7]/30" : "border-[#E0DED8]"}`}>
                <div className="flex items-center justify-between">
                  <div>
                    <div className="flex items-center gap-2">
                      <p className="text-sm font-medium text-[#2C2C2A]">{r.source_file_name || "Report"}</p>
                      {i === 0 && <span className="px-1.5 py-0.5 text-[9px] font-semibold bg-[#534AB7] text-white rounded">LATEST</span>}
                    </div>
                    <p className="text-xs text-[#9E9C95] mt-0.5">
                      {fmtDate(r.report_start_date)} – {fmtDate(r.report_end_date)} • Uploaded {fmtDateTime(r.uploaded_at)}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`px-2 py-0.5 text-[10px] font-medium rounded-full ${
                      r.ai_summary_status === "completed" ? "bg-green-50 text-green-700" :
                      r.ai_summary_status === "generating" ? "bg-yellow-50 text-yellow-700" :
                      "bg-gray-50 text-gray-600"
                    }`}>
                      {r.ai_summary_status === "completed" ? "AI Report Ready" : r.ai_summary_status === "generating" ? "Analyzing..." : "No AI Report"}
                    </span>
                  </div>
                </div>
                {/* Quick metrics preview */}
                {r.raw_metrics_json?.totals && (
                  <div className="flex gap-4 mt-3 pt-3 border-t border-[#E0DED8]">
                    {r.raw_metrics_json.totals.spend != null && (
                      <div className="text-xs"><span className="text-[#9E9C95]">Spend:</span> <span className="font-medium text-[#2C2C2A]">{fmt(r.raw_metrics_json.totals.spend, "$")}</span></div>
                    )}
                    {r.raw_metrics_json.totals.impressions != null && (
                      <div className="text-xs"><span className="text-[#9E9C95]">Impressions:</span> <span className="font-medium text-[#2C2C2A]">{fmtInt(r.raw_metrics_json.totals.impressions)}</span></div>
                    )}
                    {r.raw_metrics_json.totals.clicks != null && (
                      <div className="text-xs"><span className="text-[#9E9C95]">Clicks:</span> <span className="font-medium text-[#2C2C2A]">{fmtInt(r.raw_metrics_json.totals.clicks)}</span></div>
                    )}
                    {r.raw_metrics_json.totals.ctr != null && (
                      <div className="text-xs"><span className="text-[#9E9C95]">CTR:</span> <span className="font-medium text-[#2C2C2A]">{r.raw_metrics_json.totals.ctr}%</span></div>
                    )}
                  </div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  );
}
