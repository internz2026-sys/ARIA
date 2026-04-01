"use client";

import React, { createContext, useContext, useState, useEffect, useCallback, useRef } from "react";
import { API_URL, authFetch } from "./api";
import { AGENTS, type OfficeAgent } from "./office-config";
import { useAgentStatus } from "./socket";

const POLL_INTERVAL = 5000;

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

interface OfficeAgentsState {
  agents: OfficeAgent[];
  loaded: boolean;
  fetchAgents: () => void;
}

const OfficeAgentsContext = createContext<OfficeAgentsState | null>(null);

export function OfficeAgentsProvider({ children }: { children: React.ReactNode }) {
  const [agents, setAgents] = useState<OfficeAgent[]>(AGENTS);
  const [loaded, setLoaded] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const tenantRef = useRef("");

  const fetchAgents = useCallback(() => {
    const tid = tenantRef.current || (typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") : "") || "";
    tenantRef.current = tid;
    if (!tid) return;
    authFetch(`${API_URL}/api/office/agents/${tid}`)
      .then((r) => r.json())
      .then((data) => {
        if (data.agents) {
          setAgents(mergeAgents(data.agents));
          setLoaded(true);
        }
      })
      .catch(() => {});
  }, []);

  // Initial fetch + polling
  useEffect(() => {
    fetchAgents();
    pollRef.current = setInterval(fetchAgents, POLL_INTERVAL);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [fetchAgents]);

  // Socket.IO layer
  const tid = (typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") : "") || "";
  const liveStatuses = useAgentStatus(tid);

  // Merge live socket statuses on top
  const finalAgents = React.useMemo(() => {
    return agents.map((agent) => {
      const live = liveStatuses[agent.id];
      if (live && live.status !== "idle") {
        return {
          ...agent,
          status: live.status,
          currentTask: live.current_task,
          lastUpdated: live.last_updated,
        };
      }
      return agent;
    });
  }, [agents, liveStatuses]);

  const value = { agents: finalAgents, loaded, fetchAgents };
  return React.createElement(OfficeAgentsContext.Provider, { value }, children);
}

export function useOfficeAgents(): OfficeAgentsState {
  const ctx = useContext(OfficeAgentsContext);
  if (!ctx) throw new Error("useOfficeAgents must be used within <OfficeAgentsProvider>");
  return ctx;
}
