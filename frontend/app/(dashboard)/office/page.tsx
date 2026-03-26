"use client";

import { useEffect, useRef, useMemo, useState, useCallback } from "react";
import { AGENTS, type OfficeAgent, type AgentStatus } from "@/lib/office-config";
import VirtualOffice from "@/components/virtual-office/VirtualOffice";
import AgentInfoPanel from "@/components/virtual-office/AgentInfoPanel";

import { useAgentStatus } from "@/lib/socket";
import { useCeoChat } from "@/lib/use-ceo-chat";
import { API_URL } from "@/lib/api";
import { AGENT_NAMES } from "@/lib/agent-config";

interface ActivityItem {
  agent: string;
  action: string;
}

const EMPTY_ACTIVITY: ActivityItem[] = [
  { agent: "ARIA", action: "No recent activity — ask the CEO to assign tasks to get started" },
];

const POLL_INTERVAL = 5000; // Poll every 5s for task-based status

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

  // ── Direct chat-to-office link (instant, no backend round-trip) ──
  const { sending, messages } = useCeoChat();
  const [chatOverrides, setChatOverrides] = useState<Record<string, AgentStatus>>({});
  const prevMsgCount = useRef(0);

  // When user sends a message — ALL agents huddle at the meeting room
  useEffect(() => {
    if (sending) {
      const overrides: Record<string, AgentStatus> = {};
      for (const a of AGENTS) { if (!a.isNpc) overrides[a.id] = "running"; }
      setChatOverrides(overrides);
    }
  }, [sending]);

  // When CEO responds: delegated agents go work, rest STAY at meeting
  useEffect(() => {
    if (messages.length <= prevMsgCount.current) {
      prevMsgCount.current = messages.length;
      return;
    }
    prevMsgCount.current = messages.length;
    const last = messages[messages.length - 1];

    if (last?.role === "assistant" && last.delegations && last.delegations.length > 0) {
      // Delegated agents leave meeting and go work at their desks
      setChatOverrides((prev) => {
        const next = { ...prev };
        for (const d of last.delegations!) {
          next[d.agent] = "working";
        }
        return next;
      });
    }
    // No delegations → everyone stays at meeting (overrides unchanged)
  }, [messages]);

  // End meeting: clear overrides when chat goes quiet (10s after last response)
  useEffect(() => {
    if (sending) return; // still waiting for response
    const hasRunning = Object.values(chatOverrides).some((s) => s === "running");
    if (!hasRunning) return;
    const timer = setTimeout(() => {
      setChatOverrides((prev) => {
        // Only clear "running" agents (keep "working" ones)
        const next: Record<string, AgentStatus> = {};
        for (const [id, status] of Object.entries(prev)) {
          if (status === "working") next[id] = status;
        }
        return next;
      });
    }, 15000); // 15s after last response, non-delegated agents leave
    return () => clearTimeout(timer);
  }, [sending, chatOverrides]);

  // ── REST polling for task-based statuses (in_progress tasks) ──
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

    // Poll for task-based status updates
    pollRef.current = setInterval(() => fetchAgents(tid), POLL_INTERVAL);

    // Fetch real activity
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

  // Socket.IO as bonus instant layer (may not work on Railway)
  const liveStatuses = useAgentStatus(tenantId);

  // Clear "working" chat overrides when polled status goes idle (task moved to done)
  useEffect(() => {
    const toRemove: string[] = [];
    for (const [id, status] of Object.entries(chatOverrides)) {
      if (status !== "working") continue;
      const polled = agents.find((a) => a.id === id);
      if (polled && polled.status === "idle") toRemove.push(id);
    }
    if (toRemove.length > 0) {
      setChatOverrides((prev) => {
        const next = { ...prev };
        for (const id of toRemove) delete next[id];
        return next;
      });
    }
  }, [agents, chatOverrides]);

  // Merge: chat overrides > socket > polled REST > defaults
  const finalAgents = useMemo(() => {
    return agents.map((agent) => {
      // 1. Chat-driven overrides (highest priority — instant)
      const chatStatus = chatOverrides[agent.id];
      if (chatStatus) {
        return { ...agent, status: chatStatus };
      }
      // 2. Socket.IO live status
      const live = liveStatuses[agent.id];
      if (live) {
        return {
          ...agent,
          status: live.status,
          currentTask: live.current_task,
          lastUpdated: live.last_updated,
        };
      }
      // 3. Polled REST status (already in agents)
      return agent;
    });
  }, [agents, chatOverrides, liveStatuses]);

  useEffect(() => {
    if (!selectedAgent) return;
    const updated = finalAgents.find((a) => a.id === selectedAgent.id);
    if (updated) setSelectedAgent(updated);
  }, [finalAgents]); // eslint-disable-line react-hooks/exhaustive-deps

  function handleAgentClick(agentId: string) {
    const agent = finalAgents.find((a) => a.id === agentId) || null;
    setSelectedAgent(agent);
  }

  return (
    <>
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
              <span className="w-2 h-2 rounded-full bg-[#534AB7]" /> In Meeting
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
            <VirtualOffice agents={finalAgents} onAgentClick={handleAgentClick} />
          )}
        </div>
      </div>

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
          animation: marquee 600s linear infinite;
        }
        .animate-marquee:hover {
          animation-play-state: paused;
        }
      `}</style>
    </>
  );
}
