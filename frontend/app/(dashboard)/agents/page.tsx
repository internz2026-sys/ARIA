"use client";

import React, { useState } from "react";
import { API_URL, authFetch } from "@/lib/api";
import { AGENT_DEFS } from "@/lib/agent-config";

/* ───────── Page-specific icon SVGs for each agent ───────── */
const AGENT_ICONS: Record<string, React.ReactNode> = {
  ceo: (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth={1.6} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M11.48 3.499a.562.562 0 011.04 0l2.125 5.111a.563.563 0 00.475.345l5.518.442c.499.04.701.663.321.988l-4.204 3.602a.563.563 0 00-.182.557l1.285 5.385a.562.562 0 01-.84.61l-4.725-2.885a.563.563 0 00-.586 0L6.982 20.54a.562.562 0 01-.84-.61l1.285-5.386a.562.562 0 00-.182-.557l-4.204-3.602a.563.563 0 01.321-.988l5.518-.442a.563.563 0 00.475-.345L11.48 3.5z" />
    </svg>
  ),
  content_writer: (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth={1.6} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zm0 0L19.5 7.125M18 14v4.75A2.25 2.25 0 0115.75 21H5.25A2.25 2.25 0 013 18.75V8.25A2.25 2.25 0 015.25 6H10" />
    </svg>
  ),
  email_marketer: (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth={1.6} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
    </svg>
  ),
  social_manager: (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth={1.6} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
    </svg>
  ),
  ad_strategist: (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth={1.6} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 3v11.25A2.25 2.25 0 006 16.5h2.25M3.75 3h-1.5m1.5 0h16.5m0 0h1.5m-1.5 0v11.25A2.25 2.25 0 0118 16.5h-2.25m-7.5 0h7.5m-7.5 0l-1 3m8.5-3l1 3m0 0l.5 1.5m-.5-1.5h-9.5m0 0l-.5 1.5M9 11.25v1.5M12 9v3.75m3-6v6" />
    </svg>
  ),
  media: (
    <svg className="w-6 h-6" fill="none" stroke="currentColor" strokeWidth={1.6} viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 15.75l5.159-5.159a2.25 2.25 0 013.182 0l5.159 5.159m-1.5-1.5l1.409-1.409a2.25 2.25 0 013.182 0l2.909 2.909m-18 3.75h16.5a1.5 1.5 0 001.5-1.5V6a1.5 1.5 0 00-1.5-1.5H3.75A1.5 1.5 0 002.25 6v12a1.5 1.5 0 001.5 1.5zm10.5-11.25h.008v.008h-.008V8.25zm.375 0a.375.375 0 11-.75 0 .375.375 0 01.75 0z" />
    </svg>
  ),
};

/* ───────── Merge shared definitions with page-specific icons ───────── */
const AGENTS = AGENT_DEFS.map((a) => ({ ...a, icon: AGENT_ICONS[a.slug] }));

type AgentStatus = "active" | "idle" | "running" | "paused" | "error";

interface AgentState {
  slug: string;
  status: AgentStatus;
  lastRun: string | null;
}

const statusDisplay: Record<AgentStatus, { label: string; color: string; bg: string; pulse: boolean }> = {
  active: { label: "Active", color: "#1D9E75", bg: "#E6F7F0", pulse: false },
  running: { label: "Running", color: "#534AB7", bg: "#EEEDFE", pulse: true },
  idle: { label: "Idle", color: "#5F5E5A", bg: "#F8F8F6", pulse: false },
  paused: { label: "Paused", color: "#D85A30", bg: "#FDEEE8", pulse: false },
  error: { label: "Error", color: "#D85A30", bg: "#FDEEE8", pulse: false },
};

export default function AgentsPage() {
  const [agentStates, setAgentStates] = useState<Record<string, AgentState>>(
    Object.fromEntries(
      AGENTS.map((a) => [a.slug, { slug: a.slug, status: "idle" as AgentStatus, lastRun: null }])
    )
  );
  async function handleToggle(slug: string) {
    const current = agentStates[slug];
    const newStatus = current.status === "paused" ? "idle" : "paused";
    setAgentStates((prev) => ({
      ...prev,
      [slug]: { ...prev[slug], status: newStatus },
    }));

    try {
      const action = newStatus === "paused" ? "pause" : "resume";
      const tenantId = localStorage.getItem("aria_tenant_id") || "";
      await authFetch(`${API_URL}/api/agents/${tenantId}/${slug}/${action}`, { method: "POST" });
    } catch {
      // revert on error
      setAgentStates((prev) => ({
        ...prev,
        [slug]: { ...prev[slug], status: current.status },
      }));
    }
  }

  return (
    <div className="max-w-screen-xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">Marketing Agents</h1>
        <p className="text-sm text-[#5F5E5A] mt-1">Your AI marketing team — 6 agents working together</p>
      </div>

      {/* Org Chart Header */}
      <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
        <div className="flex items-center gap-3 mb-4">
          <img src="/logo.webp" alt="ARIA" className="h-8 w-8 rounded-full object-cover" />
          <div>
            <h2 className="text-sm font-semibold text-[#2C2C2A]">ARIA Marketing Team</h2>
            <p className="text-xs text-[#5F5E5A]">CEO orchestrates all agents</p>
          </div>
        </div>

        {/* Visual org chart */}
        <div className="flex flex-col items-center">
          {/* CEO at top */}
          <div className="flex items-center gap-2 px-4 py-2 rounded-lg border-2 border-[#534AB7] bg-[#EEEDFE]">
            <span className="text-xs font-semibold text-[#534AB7]">ARIA CEO</span>
          </div>
          {/* Connector line */}
          <div className="w-px h-6 bg-[#E0DED8]" />
          {/* Horizontal connector */}
          <div className="flex items-start">
            <div className="flex items-center gap-0">
              {AGENTS.slice(1).map((agent, i) => (
                <div key={agent.slug} className="flex flex-col items-center">
                  {/* Top connector */}
                  <div className="flex items-center">
                    {i === 0 && <div className="w-[60px] h-px bg-[#E0DED8]" />}
                    <div className="w-px h-4 bg-[#E0DED8]" />
                    {i < AGENTS.length - 2 && <div className="w-[60px] h-px bg-[#E0DED8]" />}
                  </div>
                  <div
                    className="px-3 py-1.5 rounded-md border text-[10px] font-medium whitespace-nowrap"
                    style={{ borderColor: agent.color, color: agent.color }}
                  >
                    {agent.name}
                  </div>
                </div>
              ))}
            </div>
          </div>
          {/* Horizontal line across */}
          <div className="w-full max-w-md border-t border-[#E0DED8] -mt-[calc(1rem+21px)] mb-8" />
        </div>
      </div>

      {/* Agent Cards Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
        {AGENTS.map((agent) => {
          const state = agentStates[agent.slug];
          const sd = statusDisplay[state.status];
          const isOn = state.status !== "paused";

          return (
            <div
              key={agent.slug}
              className="bg-white rounded-xl border border-[#E0DED8] p-5 hover:shadow-sm transition-shadow flex flex-col"
            >
              {/* Header */}
              <div className="flex items-start justify-between mb-3">
                <div className="flex items-center gap-3">
                  <div
                    className="w-10 h-10 rounded-xl flex items-center justify-center"
                    style={{ backgroundColor: agent.color + "15", color: agent.color }}
                  >
                    {agent.icon}
                  </div>
                  <div>
                    <h3 className="text-sm font-semibold text-[#2C2C2A]">{agent.name}</h3>
                    <span className="text-[11px] text-[#5F5E5A]">{agent.role}</span>
                  </div>
                </div>

                {/* Status badge */}
                <div className="flex items-center gap-1.5">
                  {sd.pulse ? (
                    <span className="relative flex h-2 w-2">
                      <span
                        className="animate-ping absolute inline-flex h-full w-full rounded-full opacity-75"
                        style={{ backgroundColor: sd.color }}
                      />
                      <span className="relative inline-flex rounded-full h-2 w-2" style={{ backgroundColor: sd.color }} />
                    </span>
                  ) : (
                    <span className="h-2 w-2 rounded-full" style={{ backgroundColor: sd.color }} />
                  )}
                  <span
                    className="text-[11px] font-medium px-2 py-0.5 rounded-full"
                    style={{ color: sd.color, backgroundColor: sd.bg }}
                  >
                    {sd.label}
                  </span>
                </div>
              </div>

              {/* Description */}
              <p className="text-xs text-[#5F5E5A] leading-relaxed mb-3 flex-1">{agent.description}</p>

              {/* Schedule + last run */}
              <div className="space-y-1.5 mb-4">
                <div className="flex items-center gap-2 text-[11px] text-[#5F5E5A]">
                  <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <span>{agent.schedule}</span>
                </div>
                {state.lastRun && (
                  <div className="flex items-center gap-2 text-[11px] text-[#1D9E75]">
                    <svg className="w-3.5 h-3.5 shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                    </svg>
                    <span>Last run: {state.lastRun}</span>
                  </div>
                )}
              </div>

              {/* Actions */}
              <div className="flex items-center pt-3 border-t border-[#E0DED8]">
                {/* Toggle */}
                <button
                  onClick={() => handleToggle(agent.slug)}
                  className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors ${
                    isOn ? "bg-[#1D9E75]" : "bg-[#E0DED8]"
                  }`}
                >
                  <span
                    className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${
                      isOn ? "translate-x-[18px]" : "translate-x-[3px]"
                    }`}
                  />
                </button>
              </div>
            </div>
          );
        })}
      </div>

    </div>
  );
}
