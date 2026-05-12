"use client";

import React, { useState, useEffect, useCallback } from "react";
import { usage as usageApi } from "@/lib/api";

const AGENT_LABELS: Record<string, { name: string; color: string }> = {
  ceo: { name: "CEO Strategist", color: "#534AB7" },
  content_writer: { name: "Content Writer", color: "#1D9E75" },
  email_marketer: { name: "Email Marketer", color: "#BA7517" },
  social_manager: { name: "Social Manager", color: "#D85A30" },
  ad_strategist: { name: "Ad Strategist", color: "#5F5E5A" },
  media: { name: "Media Designer", color: "#E4407B" },
};

function UsageBar({
  used,
  limit,
  color = "#534AB7",
  label,
}: {
  used: number;
  limit: number;
  color?: string;
  label?: string;
}) {
  if (limit === -1) {
    return (
      <div>
        {label && <p className="text-[11px] font-medium text-[#5F5E5A] mb-1.5">{label}</p>}
        <div className="h-2.5 bg-[#F0EFEC] rounded-full overflow-hidden">
          <div className="h-full rounded-full transition-all duration-500" style={{ width: "100%", backgroundColor: color, opacity: 0.4 }} />
        </div>
        <div className="flex items-center justify-between mt-1">
          <p className="text-[10px] text-[#9E9C95]">{used.toLocaleString()} used</p>
          <p className="text-[10px] font-semibold text-[#1D9E75]">Unlimited</p>
        </div>
      </div>
    );
  }
  const pct = limit > 0 ? Math.min((used / limit) * 100, 100) : 0;
  const isHigh = pct >= 80;
  return (
    <div>
      {label && <p className="text-[11px] font-medium text-[#5F5E5A] mb-1.5">{label}</p>}
      <div className="h-2.5 bg-[#F0EFEC] rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all duration-500"
          style={{ width: `${pct}%`, backgroundColor: isHigh ? "#D85A30" : color }}
        />
      </div>
      <div className="flex items-center justify-between mt-1">
        <p className="text-[10px] text-[#9E9C95]">
          {used.toLocaleString()} / {limit.toLocaleString()}
        </p>
        <p className={`text-[10px] font-semibold ${isHigh ? "text-[#D85A30]" : "text-[#9E9C95]"}`}>
          {Math.round(pct)}%
        </p>
      </div>
    </div>
  );
}

export default function UsagePage() {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const tenantId =
    typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchUsage = useCallback(async () => {
    if (!tenantId) return;
    try {
      const d = await usageApi.getDashboard(tenantId);
      setData(d);
    } catch {}
    finally { setLoading(false); }
  }, [tenantId]);

  useEffect(() => { fetchUsage(); }, [fetchUsage]);
  useEffect(() => {
    const i = setInterval(fetchUsage, 15000);
    return () => clearInterval(i);
  }, [fetchUsage]);

  if (loading) {
    return (
      <div className="max-w-5xl mx-auto flex items-center justify-center min-h-[300px]">
        <div className="animate-pulse text-sm text-[#9E9C95]">Loading usage data...</div>
      </div>
    );
  }

  // New backend shape: top-level fields, per_agent dict, monthly object
  const totalRequests = data?.total_requests ?? 0;
  const totalTokens = data?.total_tokens ?? 0;
  const totalInputTokens = data?.total_input_tokens ?? 0;
  const totalOutputTokens = data?.total_output_tokens ?? 0;
  const requestLimit = data?.request_limit ?? 60;
  const tokenLimit = data?.token_limit ?? 200000;
  const perAgent = data?.per_agent ?? {};
  const monthly = data?.monthly ?? null;

  return (
    <div className="max-w-5xl mx-auto space-y-6">
      <div>
        <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">API Usage</h1>
        <p className="text-sm text-[#5F5E5A]">Monitor token consumption and rate limits across all agents</p>
      </div>

      {/* Monthly Quota — shown when backend provides it */}
      {monthly && (
        <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
          <div className="flex items-center justify-between mb-4">
            <h2 className="text-base font-semibold text-[#2C2C2A]">Monthly Quota</h2>
            {monthly.plan && (
              <span className="text-[10px] text-[#9E9C95] bg-[#F8F8F6] px-2.5 py-1 rounded-full capitalize">
                {monthly.plan} plan
              </span>
            )}
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
            <UsageBar
              used={monthly.content_used ?? 0}
              limit={monthly.content_limit ?? 0}
              color="#534AB7"
              label="Content pieces"
            />
            <UsageBar
              used={monthly.campaigns_used ?? 0}
              limit={monthly.campaigns_limit ?? 0}
              color="#1D9E75"
              label="Campaign plans"
            />
          </div>
        </div>
      )}

      {/* Overall usage KPI cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
          <p className="text-xs font-semibold text-[#5F5E5A] uppercase mb-1">Requests</p>
          <p className="text-2xl font-bold text-[#2C2C2A]">{totalRequests.toLocaleString()}</p>
          <p className="text-[10px] text-[#9E9C95]">of {requestLimit.toLocaleString()} / hour</p>
        </div>
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
          <p className="text-xs font-semibold text-[#5F5E5A] uppercase mb-1">Total Tokens</p>
          <p className="text-2xl font-bold text-[#2C2C2A]">{totalTokens.toLocaleString()}</p>
          <p className="text-[10px] text-[#9E9C95]">of {tokenLimit.toLocaleString()} / hour</p>
        </div>
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
          <p className="text-xs font-semibold text-[#5F5E5A] uppercase mb-1">Input Tokens</p>
          <p className="text-2xl font-bold text-[#534AB7]">{totalInputTokens.toLocaleString()}</p>
          <p className="text-[10px] text-[#9E9C95]">prompts + context</p>
        </div>
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
          <p className="text-xs font-semibold text-[#5F5E5A] uppercase mb-1">Output Tokens</p>
          <p className="text-2xl font-bold text-[#1D9E75]">{totalOutputTokens.toLocaleString()}</p>
          <p className="text-[10px] text-[#9E9C95]">agent responses</p>
        </div>
      </div>

      {/* Hourly limits progress bars */}
      <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-[#2C2C2A]">Hourly Limits</h2>
          <span className="text-[10px] text-[#9E9C95] bg-[#F8F8F6] px-2.5 py-1 rounded-full">
            Resets every hour (UTC)
          </span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
          <UsageBar used={totalRequests} limit={requestLimit} label="Requests" />
          <UsageBar used={totalTokens} limit={tokenLimit} label="Tokens" />
        </div>
      </div>

      {/* Per-agent breakdown */}
      <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
        <h2 className="text-base font-semibold text-[#2C2C2A] mb-5">Per-Agent Breakdown</h2>
        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
          {Object.entries(perAgent).map(([agentId, a]: [string, any]) => {
            const label = AGENT_LABELS[agentId] || { name: agentId, color: "#5F5E5A" };
            // New shape: requests/tokens/limit_requests/limit_tokens
            const agentRequests = a.requests ?? 0;
            const agentTokens = a.tokens ?? 0;
            const agentLimitRequests = a.limit_requests ?? 15;
            const agentLimitTokens = a.limit_tokens ?? 40000;
            const tokenPct =
              agentLimitTokens > 0 ? Math.round((agentTokens / agentLimitTokens) * 100) : 0;
            return (
              <div
                key={agentId}
                className="border border-[#E0DED8] rounded-xl p-4 hover:shadow-sm transition-shadow"
              >
                <div className="flex items-center gap-2 mb-3">
                  <div
                    className="w-8 h-8 rounded-lg flex items-center justify-center"
                    style={{ backgroundColor: label.color + "15" }}
                  >
                    <span
                      className="w-2.5 h-2.5 rounded-full"
                      style={{ backgroundColor: label.color }}
                    />
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-semibold text-[#2C2C2A]">{label.name}</p>
                    <p className="text-[10px] text-[#9E9C95]">{agentRequests} calls this hour</p>
                  </div>
                  {tokenPct >= 80 && (
                    <span className="text-[9px] font-bold px-1.5 py-0.5 rounded-full bg-[#FDEEE8] text-[#D85A30]">
                      {tokenPct}%
                    </span>
                  )}
                </div>
                <div className="space-y-3">
                  <UsageBar
                    used={agentRequests}
                    limit={agentLimitRequests}
                    color={label.color}
                    label="Requests"
                  />
                  <UsageBar
                    used={agentTokens}
                    limit={agentLimitTokens}
                    color={label.color}
                    label="Tokens"
                  />
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
