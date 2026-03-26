"use client";

import React from "react";

export default function ReportsPage() {
  return (
    <div className="max-w-[1400px] space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-[#2C2C2A]">Reports</h1>
        <p className="text-sm text-[#5F5E5A] mt-1">Generated reports from your marketing agents</p>
      </div>

      {/* Generate buttons */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {[
          { label: "Weekly Marketing Report", desc: "Overview of all agent activity and KPIs", agent: "ARIA CEO" },
          { label: "Content Performance", desc: "Blog posts, social, and email engagement", agent: "Content Writer" },
          { label: "Ad Campaign Report", desc: "Facebook ads performance and optimization", agent: "Ad Strategist" },
        ].map((r) => (
          <div
            key={r.label}
            className="bg-white rounded-xl border border-[#E0DED8] p-4 opacity-60"
          >
            <p className="text-sm font-semibold text-[#2C2C2A]">{r.label}</p>
            <p className="text-xs text-[#5F5E5A] mt-1">{r.desc}</p>
            <div className="flex items-center justify-between mt-3">
              <span className="text-[10px] text-[#5F5E5A]">by {r.agent}</span>
              <span className="text-xs font-medium text-[#5F5E5A]">No data yet</span>
            </div>
          </div>
        ))}
      </div>

      {/* Empty state */}
      <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[300px] flex items-center justify-center">
        <div className="text-center px-6 py-16">
          <div className="w-16 h-16 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-4">
            <svg className="w-8 h-8 text-[#E0DED8]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z" />
            </svg>
          </div>
          <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No reports yet</h3>
          <p className="text-sm text-[#5F5E5A] max-w-sm mx-auto">
            Reports will be generated as your agents complete tasks. Run your first agent to start collecting data.
          </p>
          <a href="/agents" className="inline-block mt-4 text-sm font-medium text-[#534AB7] hover:underline">Go to Agents</a>
        </div>
      </div>
    </div>
  );
}
