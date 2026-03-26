"use client";

import { useEffect, useRef, useState } from "react";
import { io, Socket } from "socket.io-client";

// ---------------------------------------------------------------------------
// Singleton socket connection (lazy-initialized)
// ---------------------------------------------------------------------------

const BACKEND_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

let socket: Socket | null = null;

export function getSocket(): Socket {
  if (!socket) {
    socket = io(BACKEND_URL, {
      autoConnect: false,
      transports: ["websocket", "polling"],
    });
  }
  if (!socket.connected) {
    socket.connect();
  }
  return socket;
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface AgentStatusPayload {
  agent_id: string;
  status: "running" | "busy" | "idle" | "working";
  current_task: string;
  last_updated: string;
}

export interface AgentEvent {
  agent_id: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// useAgentStatus — track live status of every agent for a tenant
// ---------------------------------------------------------------------------

export function useAgentStatus(tenantId: string): Record<string, AgentStatusPayload> {
  const [statuses, setStatuses] = useState<Record<string, AgentStatusPayload>>({});
  const joinedRoom = useRef<string | null>(null);

  useEffect(() => {
    if (!tenantId) return;

    const s = getSocket();

    const handleConnect = () => {
      s.emit("join_tenant", { tenant_id: tenantId });
      joinedRoom.current = tenantId;
    };

    const handleStatusChange = (payload: AgentStatusPayload) => {
      setStatuses((prev) => ({
        ...prev,
        [payload.agent_id]: payload,
      }));
    };

    s.on("connect", handleConnect);
    s.on("agent_status_change", handleStatusChange);

    // If already connected, join immediately
    if (s.connected) {
      handleConnect();
    }

    return () => {
      s.off("connect", handleConnect);
      s.off("agent_status_change", handleStatusChange);

      if (joinedRoom.current) {
        s.emit("leave_tenant", { tenant_id: joinedRoom.current });
        joinedRoom.current = null;
      }
    };
  }, [tenantId]);

  return statuses;
}

// ---------------------------------------------------------------------------
// useActivityFeed — keep the last 10 agent_event events for a tenant
// ---------------------------------------------------------------------------

const MAX_FEED_SIZE = 10;

export function useActivityFeed(tenantId: string): AgentEvent[] {
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const joinedRoom = useRef<string | null>(null);

  useEffect(() => {
    if (!tenantId) return;

    const s = getSocket();

    const handleConnect = () => {
      s.emit("join_tenant", { tenant_id: tenantId });
      joinedRoom.current = tenantId;
    };

    const handleAgentEvent = (event: AgentEvent) => {
      setEvents((prev) => [event, ...prev].slice(0, MAX_FEED_SIZE));
    };

    s.on("connect", handleConnect);
    s.on("agent_event", handleAgentEvent);

    // If already connected, join immediately
    if (s.connected) {
      handleConnect();
    }

    return () => {
      s.off("connect", handleConnect);
      s.off("agent_event", handleAgentEvent);

      if (joinedRoom.current) {
        s.emit("leave_tenant", { tenant_id: joinedRoom.current });
        joinedRoom.current = null;
      }
    };
  }, [tenantId]);

  return events;
}
