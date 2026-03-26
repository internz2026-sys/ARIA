"use client";

import { useEffect, useRef, useMemo, useState, useCallback } from "react";
import { AGENTS, type OfficeAgent } from "@/lib/office-config";
import VirtualOffice from "@/components/virtual-office/VirtualOffice";
import AgentInfoPanel from "@/components/virtual-office/AgentInfoPanel";
import OfficeKanban from "@/components/virtual-office/OfficeKanban";
import { useAgentStatus } from "@/lib/socket";
import { API_URL } from "@/lib/api";
import { AGENT_NAMES } from "@/lib/agent-config";

interface ActivityItem {
  agent: string;
  action: string;
}

const EMPTY_ACTIVITY: ActivityItem[] = [
  { agent: "ARIA", action: "No recent activity — ask the CEO to assign tasks to get started" },
];

const POLL_INTERVAL = 3000; // Poll every 3s for live updates

function mergeAgents(remoteAgents: any[]): OfficeAgent[] {
  return AGENTS.map((local) => {
    const remote = remoteAgents.find((a: any) => a.agent_id === local.id);
    if (remote) {
      return {
        ...local,
        status: remote.status || local.status,
        currentTask: remote.current_task || "",
        lastUpdated: remote.last_updated || local.lastUpdated,
      };
    }
    return local;
  });
}

export default function OfficePage() {
  const [agents, setAgents] = useState<OfficeAgent[]>(AGENTS);
  const [selectedAgent, setSelectedAgent] = useState<OfficeAgent | null>(null);
  const [loading, setLoading] = useState(true);
  const [tenantId, setTenantId] = useState<string>("");
  const [activity, setActivity] = useState<ActivityItem[]>(EMPTY_ACTIVITY);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Fetch agent statuses from REST API
  const fetchAgents = useCallback((tid: string) => {
    fetch(`${API_URL}/api/office/agents/${tid}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.agents) setAgents(mergeAgents(data.agents));
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    const tid = localStorage.getItem("aria_tenant_id");
    setTenantId(tid || "");
    if (!tid) {
      setLoading(false);
      return;
    }

    // Initial fetch
    fetch(`${API_URL}/api/office/agents/${tid}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.agents) setAgents(mergeAgents(data.agents));
        setLoading(false);
      })
      .catch(() => setLoading(false));

    // Poll for live updates (reliable fallback — works even if Socket.IO fails)
    pollRef.current = setInterval(() => fetchAgents(tid), POLL_INTERVAL);

    // Fetch real activity from tasks + inbox
    fetch(`${API_URL}/api/dashboard/${tid}/activity`)
      .then((r) => r.json())
      .then((data) => {
        if (data.activity && data.activity.length > 0) {
          setActivity(
            data.activity.map((a: any) => ({
              agent: AGENT_NAMES[a.agent] || a.agent,
              action: a.action,
            }))
          );
        }
      })
      .catch(() => {});

    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [fetchAgents]);

  // Socket.IO for instant updates (optimization on top of polling)
  const liveStatuses = useAgentStatus(tenantId);

  const agentsWithLive = useMemo(() => {
    if (Object.keys(liveStatuses).length === 0) return agents;
    return agents.map((agent) => {
      const live = liveStatuses[agent.id];
      if (!live) return agent;
      return {
        ...agent,
        status: live.status,
        currentTask: live.current_task,
        lastUpdated: live.last_updated,
      };
    });
  }, [agents, liveStatuses]);

  useEffect(() => {
    if (!selectedAgent) return;
    const updated = agentsWithLive.find((a) => a.id === selectedAgent.id);
    if (updated) setSelectedAgent(updated);
  }, [agentsWithLive]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleAgentClick(agentId: string) {
    const agent = agentsWithLive.find((a) => a.id === agentId) || null;
    setSelectedAgent(agent);
  }

  return (
    <>
      {/*
        Fixed overlay — positioned directly from sidebar edge to viewport edge.
        Bypasses <main> padding entirely. No negative margin hacks.
        Mobile: below the sticky h-14 header. Desktop: full height, after 240px sidebar.
      */}
      <div className="fixed top-14 lg:top-0 left-0 lg:left-[240px] right-0 bottom-0 flex flex-col z-20 bg-[#F8F8F6]">
        {/* Header bar */}
        <div className="flex items-center justify-between px-4 py-2 bg-[#F8F8F6] border-b border-[#E0DED8]/60 shrink-0">
          <div>
            <h1 className="text-base font-bold text-[#2C2C2A]">Virtual Office</h1>
            <p className="text-[10px] text-[#5F5E5A]">
              {agents.length} agents working across {new Set(agents.map((a) => a.department)).size} departments
            </p>
          </div>
          <div className="flex items-center gap-3 text-[10px] text-[#5F5E5A]">
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-[#1D9E75]" /> Idle
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-[#3B82F6]" /> Working
            </span>
            <span className="flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-[#EAB308]" /> In Meeting
            </span>
            <span className="flex items-center gap-1">
              <span className="text-[#FFD700] text-xs">♛</span> Opus 4.6
            </span>
          </div>
        </div>

        {/* Activity ticker */}
        <div className="relative overflow-hidden h-6 bg-[#F8F8F6] border-b border-[#E0DED8]/60 flex items-center shrink-0">
          <span className="shrink-0 px-2 text-[10px] font-semibold text-[#5F5E5A] uppercase tracking-wide bg-[#F8F8F6] z-10">
            Activity
          </span>
          <div className="flex animate-marquee whitespace-nowrap">
            {[...activity, ...activity].map((a, i) => (
              <span key={i} className="text-[11px] text-[#5F5E5A] mx-4">
                <strong className="text-[#2C2C2A]">{a.agent}</strong>
                {" — "}
                {a.action}
              </span>
            ))}
          </div>
        </div>

        {/* Canvas area */}
        <div className="flex-1 min-h-0 relative">
          {loading ? (
            <div className="absolute inset-0 flex items-center justify-center">
              <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
            </div>
          ) : (
            <VirtualOffice agents={agentsWithLive} onAgentClick={handleAgentClick} />
          )}
        </div>
      </div>

      {/* These use fixed positioning internally — render outside the office div */}
      <OfficeKanban />
      <AgentInfoPanel
        agent={selectedAgent}
        onClose={() => setSelectedAgent(null)}
      />

      <style jsx>{`
        @keyframes marquee {
          0% { transform: translateX(0); }
          100% { transform: translateX(-50%); }
        }
        .animate-marquee {
          animation: marquee 30s linear infinite;
        }
        .animate-marquee:hover {
          animation-play-state: paused;
        }
      `}</style>
    </>
  );
}
