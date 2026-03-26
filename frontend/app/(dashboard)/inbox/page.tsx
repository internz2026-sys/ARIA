"use client";

import React, { useState } from "react";

const tabs = [
  { label: "All", count: 0 },
  { label: "Content ready", count: 0 },
  { label: "Needs review", count: 0 },
  { label: "Completed", count: 0 },
];

const items: { id: number; agent: string; agentColor: string; title: string; type: string; status: string; statusColor: string; time: string; preview: string }[] = [];

export default function InboxPage() {
  const [activeTab, setActiveTab] = useState("All");

  return (
    <div className="max-w-[1400px] space-y-4">
      <h1 className="text-2xl font-semibold text-[#2C2C2A]">Inbox</h1>
      <p className="text-sm text-[#5F5E5A] -mt-2">Content and deliverables from your marketing agents</p>

      {/* Tabs */}
      <div className="flex items-center gap-1 bg-white rounded-xl border border-[#E0DED8] p-1.5 overflow-x-auto">
        {tabs.map((tab) => (
          <button key={tab.label} onClick={() => setActiveTab(tab.label)} className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${activeTab === tab.label ? "bg-[#EEEDFE] text-[#534AB7]" : "text-[#5F5E5A] hover:bg-[#F8F8F6]"}`}>
            {tab.label}
            <span className={`text-xs px-1.5 py-0.5 rounded-full ${activeTab === tab.label ? "bg-[#534AB7] text-white" : "bg-[#F8F8F6] text-[#5F5E5A]"}`}>{tab.count}</span>
          </button>
        ))}
      </div>

      {/* Content area */}
      <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[400px] flex items-center justify-center">
        <div className="text-center px-6 py-16">
          <div className="w-16 h-16 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-4">
            <svg className="w-8 h-8 text-[#E0DED8]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859M12 3v8.25m0 0l-3-3m3 3l3-3" />
            </svg>
          </div>
          <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No deliverables yet</h3>
          <p className="text-sm text-[#5F5E5A] max-w-sm mx-auto">
            When your marketing agents create content, email sequences, ad campaigns, or strategy updates, they&apos;ll appear here for you to review and copy.
          </p>
          <a href="/agents" className="inline-block mt-4 text-sm font-medium text-[#534AB7] hover:underline">
            Run an agent to get started
          </a>
        </div>
      </div>
    </div>
  );
}
