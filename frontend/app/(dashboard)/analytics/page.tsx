"use client";

import React, { useState, useEffect } from "react";
import { API_URL } from "@/lib/api";
const dateRanges = ["Last 7 days", "Last 30 days", "Last 90 days"];

export default function AnalyticsPage() {
  const [activeRange, setActiveRange] = useState("Last 7 days");
  const [funnel, setFunnel] = useState({ impressions: 0, clicks: 0, signups: 0, activated: 0, converted: 0, retained: 0 });

  useEffect(() => {
    fetch(`${API_URL}/api/analytics/demo?date_range=7d`).then(r => r.json()).then(d => d.funnel && setFunnel(d.funnel)).catch(() => {});
  }, []);

  const hasData = Object.values(funnel).some(v => v > 0);

  const funnelData = [
    { stage: "Impressions", value: funnel.impressions },
    { stage: "Clicks", value: funnel.clicks },
    { stage: "Signups", value: funnel.signups },
    { stage: "Activated", value: funnel.activated },
    { stage: "Converted", value: funnel.converted },
    { stage: "Retained", value: funnel.retained },
  ];
  const maxFunnel = Math.max(...funnelData.map(d => d.value), 1);

  return (
    <div className="max-w-[1400px] space-y-6">
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
        <h1 className="text-2xl font-semibold text-[#2C2C2A]">Analytics</h1>
        <div className="flex items-center gap-1 bg-white rounded-lg border border-[#E0DED8] p-1">
          {dateRanges.map((r) => (
            <button key={r} onClick={() => setActiveRange(r)} className={`px-3 py-1.5 rounded-md text-xs font-medium transition-colors ${activeRange === r ? "bg-[#534AB7] text-white" : "text-[#5F5E5A] hover:bg-[#F8F8F6]"}`}>
              {r}
            </button>
          ))}
        </div>
      </div>

      {!hasData ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[400px] flex items-center justify-center">
          <div className="text-center px-6 py-16">
            <div className="w-16 h-16 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-4">
              <svg className="w-8 h-8 text-[#E0DED8]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
              </svg>
            </div>
            <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No analytics data yet</h3>
            <p className="text-sm text-[#5F5E5A] max-w-sm mx-auto">
              Analytics will populate as your agents create content, send emails, and run campaigns. Start by running an agent.
            </p>
            <a href="/agents" className="inline-block mt-4 text-sm font-medium text-[#534AB7] hover:underline">Go to Agents</a>
          </div>
        </div>
      ) : (
        <>
          {/* Marketing Funnel */}
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-5">Marketing Funnel</h2>
            <div className="space-y-3">
              {funnelData.map((item, i) => (
                <div key={item.stage} className="flex items-center gap-4">
                  <div className="w-24 text-right"><span className="text-xs font-medium text-[#5F5E5A]">{item.stage}</span></div>
                  <div className="flex-1 relative">
                    <div className="h-10 bg-[#F8F8F6] rounded-lg overflow-hidden">
                      <div className="h-full rounded-lg transition-all duration-500" style={{ width: `${(item.value / maxFunnel) * 100}%`, backgroundColor: `rgba(83, 74, 183, ${1 - i * 0.12})` }} />
                    </div>
                  </div>
                  <div className="w-20"><span className="text-sm font-semibold text-[#2C2C2A]">{item.value.toLocaleString()}</span></div>
                  <div className="w-16">{i > 0 && funnelData[i-1].value > 0 && <span className="text-xs text-[#5F5E5A]">{((item.value / funnelData[i-1].value) * 100).toFixed(1)}%</span>}</div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
