"use client";

import React, { useState, useEffect, useCallback } from "react";
import { usage as usageApi } from "@/lib/api";

const AGENT_LABELS: Record<string, { name: string; color: string }> = {
  ceo: { name: "CEO Strategist", color: "#534AB7" },
  content_writer: { name: "Content Writer", color: "#1D9E75" },
  email_marketer: { name: "Email Marketer", color: "#BA7517" },
  social_manager: { name: "Social Manager", color: "#D85A30" },
  ad_strategist: { name: "Ad Strategist", color: "#5F5E5A" },
};

function UsageBar({ used, limit, color = "#534AB7", label }: { used: number; limit: number; color?: string; label?: string }) {
  const pct = limit > 0 ? Math.min((used / limit) * 100, 100) : 0;
  const isHigh = pct >= 80;
  return (
    <div>
      {label && <p className="text-[11px] font-medium text-[#5F5E5A] mb-1.5">{label}</p>}
      <div className="h-2.5 bg-[#F0EFEC] rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: isHigh ? "#D85A30" : color }} />
      </div>
      <div className="flex items-center justify-between mt-1">
        <p className="text-[10px] text-[#9E9C95]">{used.toLocaleString()} / {limit.toLocaleString()}</p>
        <p className={`text-[10px] font-semibold ${isHigh ? "text-[#D85A30]" : "text-[#9E9C95]"}`}>{Math.round(pct)}%</p>
      </div>
    </div>
  );
}

export default function UsagePage() {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchUsage = useCallback(async () => {
    if (!tenantId) return;
    try {
      const d = await usageApi.getDashboard(tenantId);
      setData(d);
    } catch {} finally { setLoading(false); }
  }, [tenantId]);

  useEffect(() => { fetchUsage(); }, [fetchUsage]);
  useEffect(() => {
    const i = setInterval(fetchUsage, 15000);
    return () => clearInterval(i);
  }, [fetchUsage]);

  if (loading) {
    return (
      <div className="max-w-[1000px] flex items-center justify-center min-h-[300px]">
        <div className="animate-pulse text-sm text-[#9E9C95]">Loading usage data...</div>
      </div>
    );
  }

  const t = data?.tenant || {};
  const agents = data?.agents || {};

  return (
    <div className="max-w-[1000px] space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-[#2C2C2A]">API Usage</h1>
        <p className="text-sm text-[#5F5E5A]">Monitor token consumption and rate limits across all agents</p>
      </div>

      {/* Overall usage cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
          <p className="text-xs font-semibold text-[#5F5E5A] uppercase mb-1">Requests</p>
          <p className="text-2xl font-bold text-[#2C2C2A]">{(t.requests || 0).toLocaleString()}</p>
          <p className="text-[10px] text-[#9E9C95]">of {(t.request_limit || 0).toLocaleString()} / hour</p>
        </div>
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
          <p className="text-xs font-semibold text-[#5F5E5A] uppercase mb-1">Total Tokens</p>
          <p className="text-2xl font-bold text-[#2C2C2A]">{(t.total_tokens || 0).toLocaleString()}</p>
          <p className="text-[10px] text-[#9E9C95]">of {(t.token_limit || 0).toLocaleString()} / hour</p>
        </div>
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
          <p className="text-xs font-semibold text-[#5F5E5A] uppercase mb-1">Input Tokens</p>
          <p className="text-2xl font-bold text-[#534AB7]">{(t.input_tokens || 0).toLocaleString()}</p>
          <p className="text-[10px] text-[#9E9C95]">prompts + context</p>
        </div>
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
          <p className="text-xs font-semibold text-[#5F5E5A] uppercase mb-1">Output Tokens</p>
          <p className="text-2xl font-bold text-[#1D9E75]">{(t.output_tokens || 0).toLocaleString()}</p>
          <p className="text-[10px] text-[#9E9C95]">agent responses</p>
        </div>
      </div>

      {/* Overall progress bars */}
      <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-[#2C2C2A]">Hourly Limits</h2>
          <span className="text-[10px] text-[#9E9C95] bg-[#F8F8F6] px-2.5 py-1 rounded-full">Resets every hour (UTC)</span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
          <UsageBar used={t.requests || 0} limit={t.request_limit || 60} label="Requests" />
          <UsageBar used={t.total_tokens || 0} limit={t.token_limit || 200000} label="Tokens" />
        </div>
      </div>

      {/* Per-agent breakdown */}
      <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
        <h2 className="text-base font-semibold text-[#2C2C2A] mb-5">Per-Agent Breakdown</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {Object.entries(agents).map(([agentId, a]: [string, any]) => {
            const label = AGENT_LABELS[agentId] || { name: agentId, color: "#5F5E5A" };
            const tokenPct = a.token_limit > 0 ? Math.round((a.total_tokens / a.token_limit) * 100) : 0;
            return (
              <div key={agentId} className="border border-[#E0DED8] rounded-xl p-4 hover:shadow-sm transition-shadow">
                <div className="flex items-center gap-2 mb-3">
                  <div className="w-8 h-8 rounded-lg flex items-center justify-center" style={{ backgroundColor: label.color + "15" }}>
                    <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: label.color }} />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-semibold text-[#2C2C2A]">{label.name}</p>
                    <p className="text-[10px] text-[#9E9C95]">{a.requests || 0} calls this hour</p>
                  </div>
                  {tokenPct >= 80 && (
                    <span className="text-[9px] font-bold px-1.5 py-0.5 rounded-full bg-[#FDEEE8] text-[#D85A30]">{tokenPct}%</span>
                  )}
                </div>
                <div className="space-y-3">
                  <UsageBar used={a.requests || 0} limit={a.request_limit || 15} color={label.color} label="Requests" />
                  <UsageBar used={a.total_tokens || 0} limit={a.token_limit || 40000} color={label.color} label="Tokens" />
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
