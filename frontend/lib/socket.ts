"use client";

import { useEffect, useRef, useState } from "react";
import { io, Socket } from "socket.io-client";
import { supabase } from "@/lib/supabase";

// ---------------------------------------------------------------------------
// Singleton socket connection (lazy-initialized)
// ---------------------------------------------------------------------------

const BACKEND_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

let socket: Socket | null = null;

// Cache the most recent access token. Refreshed via onAuthStateChange so
// reconnects after token rotation use the new value. The auth callback
// passed to socket.io-client fires on every connect attempt, so we just
// hand it whatever's current.
let currentToken: string = "";

async function refreshToken(): Promise<string> {
  try {
    const { data } = await supabase.auth.getSession();
    currentToken = data.session?.access_token || "";
  } catch {
    currentToken = "";
  }
  return currentToken;
}

// Watch for sign-in / sign-out / token-refresh events. On a real change
// we drop the existing connection so it reconnects with the new token.
if (typeof window !== "undefined") {
  supabase.auth.onAuthStateChange((_event, session) => {
    const next = session?.access_token || "";
    if (next !== currentToken) {
      currentToken = next;
      if (socket?.connected) {
        socket.disconnect();
        socket.connect();
      }
    }
  });
}

export function getSocket(): Socket {
  if (!socket) {
    socket = io(BACKEND_URL, {
      autoConnect: false,
      transports: ["websocket", "polling"],
      // Async auth callback — socket.io-client invokes this on every
      // connect attempt. We fetch the freshest token from Supabase
      // each time so a rotated/refreshed JWT is picked up without a
      // page reload. Returning an empty token lets the backend's
      // dev-mode bypass (SUPABASE_JWT_SECRET unset) keep working
      // locally; in prod the backend will reject and the client will
      // see an error on connect.
      auth: (cb: (data: { token: string }) => void) => {
        refreshToken().then((token) => cb({ token })).catch(() => cb({ token: "" }));
      },
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

// ---------------------------------------------------------------------------
// useTaskUpdates — listen for real-time task status changes
// ---------------------------------------------------------------------------

export interface TaskUpdatePayload {
  id: string;
  agent: string;
  status: string;
  task: string;
}

export function useTaskUpdates(tenantId: string): TaskUpdatePayload | null {
  const [update, setUpdate] = useState<TaskUpdatePayload | null>(null);
  const joinedRoom = useRef<string | null>(null);

  useEffect(() => {
    if (!tenantId) return;

    const s = getSocket();

    const handleConnect = () => {
      s.emit("join_tenant", { tenant_id: tenantId });
      joinedRoom.current = tenantId;
    };

    const handleTaskUpdate = (payload: TaskUpdatePayload) => {
      setUpdate(payload);
    };

    s.on("connect", handleConnect);
    s.on("task_updated", handleTaskUpdate);

    if (s.connected) {
      handleConnect();
    }

    return () => {
      s.off("connect", handleConnect);
      s.off("task_updated", handleTaskUpdate);

      if (joinedRoom.current) {
        s.emit("leave_tenant", { tenant_id: joinedRoom.current });
        joinedRoom.current = null;
      }
    };
  }, [tenantId]);

  return update;
}

// ---------------------------------------------------------------------------
// useActivityFeed — keep the last 10 agent_event events for a tenant
// ---------------------------------------------------------------------------

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
