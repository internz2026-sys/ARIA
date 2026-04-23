"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { cn } from "@/lib/utils";
import { API_URL, authFetch } from "@/lib/api";
import type { OfficeAgent, AgentStatus } from "@/lib/office-config";

interface ActivityItem {
  kind: "log" | "inbox";
  action: string;
  status: string;
  summary: string;
  timestamp: string | null;
}

function formatRelative(iso: string | undefined | null): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "—";
  const diff = Math.max(0, Date.now() - ts);
  const s = Math.floor(diff / 1000);
  if (s < 45) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(ts).toLocaleDateString();
}

interface AgentInfoPanelProps {
  agent: OfficeAgent | null;
  onClose: () => void;
}

const MODEL_DISPLAY: Record<
  string,
  { label: string; badgeBg: string; badgeText: string }
> = {
  "opus-4-6": {
    label: "Opus 4.6",
    badgeBg: "bg-[#BA7517]/15",
    badgeText: "text-[#BA7517]",
  },
  "sonnet-4-6": {
    label: "Sonnet 4.6",
    badgeBg: "bg-[#534AB7]/15",
    badgeText: "text-[#534AB7]",
  },
  "haiku-4-5": {
    label: "Haiku 4.5",
    badgeBg: "bg-[#1D9E75]/15",
    badgeText: "text-[#1D9E75]",
  },
};

const STATUS_CONFIG: Record<
  AgentStatus,
  { label: string; dotColor: string; pulse: boolean }
> = {
  running: { label: "In Meeting", dotColor: "bg-[#534AB7]", pulse: true },
  working: { label: "Working", dotColor: "bg-[#3B82F6]", pulse: true },
  busy: { label: "Busy", dotColor: "bg-[#BA7517]", pulse: false },
  idle: { label: "Idle", dotColor: "bg-[#1D9E75]", pulse: false },
};

function CrownIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="20"
      height="20"
      viewBox="0 0 20 20"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        d="M3 15h14v2H3v-2ZM4 14l-2-8 5 4 3-5 3 5 5-4-2 8H4Z"
        fill="#BA7517"
      />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg
      width="20"
      height="20"
      viewBox="0 0 20 20"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path
        d="M6 6l8 8M14 6l-8 8"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
      />
    </svg>
  );
}

export default function AgentInfoPanel({ agent, onClose }: AgentInfoPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const router = useRouter();
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [activityLoading, setActivityLoading] = useState(false);
  const [running, setRunning] = useState(false);

  // Close on Escape key
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    if (agent) {
      document.addEventListener("keydown", handleKeyDown);
      return () => document.removeEventListener("keydown", handleKeyDown);
    }
  }, [agent, onClose]);

  // Fetch recent activity for this agent + refresh every 8s while open.
  // Refresh keeps the panel in sync with whatever the agent is doing
  // right now (walking sprite + currentTask are already live via
  // socket; the log/inbox writes are polled).
  useEffect(() => {
    if (!agent) return;
    const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") : null;
    if (!tenantId) return;
    let cancelled = false;
    const fetchActivity = async () => {
      setActivityLoading(true);
      try {
        const res = await authFetch(
          `${API_URL}/api/office/agents/${tenantId}/${encodeURIComponent(agent.id)}/activity?limit=8`
        );
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled) setActivity(Array.isArray(data.items) ? data.items : []);
      } catch {
        // swallow — panel stays on last known state
      } finally {
        if (!cancelled) setActivityLoading(false);
      }
    };
    fetchActivity();
    const id = setInterval(fetchActivity, 8000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [agent?.id]);

  const handleRunAgent = async () => {
    if (!agent) return;
    const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") : null;
    if (!tenantId) return;
    setRunning(true);
    try {
      await authFetch(`${API_URL}/api/agents/${tenantId}/${encodeURIComponent(agent.id)}/run`, {
        method: "POST",
      });
    } catch {
      // Status updates arrive via socket; nothing more to do here
    } finally {
      setRunning(false);
    }
  };

  const handleViewLogs = () => {
    if (!agent) return;
    router.push(`/agents?focus=${encodeURIComponent(agent.id)}`);
    onClose();
  };

  const isOpen = agent !== null;
  const modelInfo = agent?.model ? MODEL_DISPLAY[agent.model] : null;
  const statusInfo = agent ? STATUS_CONFIG[agent.status] : null;

  return (
    <>
      {/* Backdrop overlay */}
      <div
        className={cn(
          "fixed inset-0 z-40 bg-black/20 transition-opacity duration-300",
          isOpen ? "opacity-100 pointer-events-auto" : "opacity-0 pointer-events-none"
        )}
        onClick={onClose}
        aria-hidden="true"
      />

      {/* Slide-out panel */}
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={agent ? `${agent.name} details` : "Agent details"}
        className={cn(
          "fixed top-0 right-0 z-50 h-full w-[calc(100vw-2rem)] max-w-[360px] bg-white border-l border-gray-200 shadow-xl",
          "flex flex-col overflow-y-auto",
          "transition-transform duration-300 ease-in-out",
          isOpen ? "translate-x-0" : "translate-x-full"
        )}
      >
        {agent && statusInfo && (
          <>
            {/* Header with close button */}
            <div className="flex items-start justify-between p-5 pb-0">
              <div className="flex-1" />
              <button
                onClick={onClose}
                className="flex items-center justify-center w-8 h-8 rounded-lg text-gray-400 hover:text-gray-600 hover:bg-gray-100 transition-colors"
                aria-label="Close panel"
              >
                <CloseIcon />
              </button>
            </div>

            {/* Agent avatar + identity */}
            <div className="flex flex-col items-center px-5 pt-2 pb-5">
              {/* Pixel-art style avatar */}
              <div className="relative mb-3">
                <div
                  className="w-20 h-20 rounded-2xl flex items-center justify-center"
                  style={{ backgroundColor: agent.color }}
                >
                  {/* Simplified pixel face using inner divs */}
                  <div className="relative w-12 h-12">
                    {/* Eyes */}
                    <div className="absolute top-3 left-2 w-2 h-2 rounded-sm bg-white" />
                    <div className="absolute top-3 right-2 w-2 h-2 rounded-sm bg-white" />
                    {/* Mouth */}
                    <div className="absolute bottom-2 left-1/2 -translate-x-1/2 w-4 h-1.5 rounded-sm bg-white/80" />
                  </div>
                </div>
                {/* Crown for Opus */}
                {agent.hasCrown && (
                  <div className="absolute -top-3 left-1/2 -translate-x-1/2">
                    <CrownIcon className="drop-shadow-sm" />
                  </div>
                )}
              </div>

              {/* Name */}
              <h2 className="text-lg font-bold text-gray-900 text-center">
                {agent.name}
              </h2>

              {/* Role */}
              <p className="text-sm text-gray-500 text-center mt-0.5">
                {agent.role}
              </p>

              {/* Model badge — only for AI agents */}
              {modelInfo && (
                <span
                  className={cn(
                    "inline-flex items-center gap-1 mt-3 px-2.5 py-1 rounded-full text-xs font-semibold",
                    modelInfo.badgeBg,
                    modelInfo.badgeText
                  )}
                >
                  {agent.hasCrown && (
                    <CrownIcon className="w-3.5 h-3.5" />
                  )}
                  {modelInfo.label}
                </span>
              )}
              {agent.isNpc && (
                <span className="inline-flex items-center mt-3 px-2.5 py-1 rounded-full text-xs font-medium bg-gray-100 text-gray-500">
                  Office Staff
                </span>
              )}
            </div>

            {/* Divider */}
            <div className="mx-5 border-t border-gray-100" />

            {/* Info rows */}
            <div className="flex flex-col gap-4 px-5 py-5">
              {/* Status */}
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-gray-500">Status</span>
                <span className="inline-flex items-center gap-1.5 text-sm font-medium text-gray-900">
                  <span className="relative flex h-2.5 w-2.5">
                    {statusInfo.pulse && (
                      <span
                        className={cn(
                          "animate-ping absolute inline-flex h-full w-full rounded-full opacity-75",
                          statusInfo.dotColor
                        )}
                      />
                    )}
                    <span
                      className={cn(
                        "relative inline-flex rounded-full h-2.5 w-2.5",
                        statusInfo.dotColor
                      )}
                    />
                  </span>
                  {statusInfo.label}
                </span>
              </div>

              {/* Department */}
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-gray-500">Department</span>
                <span className="inline-flex items-center px-2 py-0.5 rounded-md bg-gray-100 text-xs font-medium text-gray-700">
                  {agent.department}
                </span>
              </div>

              {/* Current task */}
              <div className="flex flex-col gap-1">
                <span className="text-sm font-medium text-gray-500">Current Task</span>
                <p className="text-sm text-gray-900 bg-gray-50 rounded-lg px-3 py-2">
                  {agent.currentTask || "No active task"}
                </p>
              </div>

              {/* Last updated */}
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-gray-500">Last Updated</span>
                <span className="text-sm text-gray-700">{formatRelative(agent.lastUpdated)}</span>
              </div>
            </div>

            {/* Divider */}
            <div className="mx-5 border-t border-gray-100" />

            {/* Recent Activity */}
            <div className="flex flex-col gap-3 px-5 py-5">
              <h3 className="text-sm font-semibold text-gray-900">Recent Activity</h3>
              {activity.length === 0 ? (
                <div className="flex items-center justify-center py-6 text-sm text-gray-400 bg-gray-50 rounded-lg">
                  {activityLoading ? "Loading…" : "No recent activity"}
                </div>
              ) : (
                <ul className="flex flex-col gap-2">
                  {activity.map((it, idx) => (
                    <li
                      key={`${it.kind}-${it.timestamp}-${idx}`}
                      className="flex items-start gap-2 px-3 py-2 rounded-lg bg-gray-50 border border-gray-100"
                    >
                      <span
                        className={cn(
                          "mt-1 inline-block w-1.5 h-1.5 rounded-full flex-shrink-0",
                          it.status === "completed"
                            ? "bg-[#1D9E75]"
                            : it.status === "failed" || it.status === "error"
                            ? "bg-red-500"
                            : it.status === "in_progress" || it.status === "working"
                            ? "bg-[#3B82F6]"
                            : "bg-gray-400"
                        )}
                      />
                      <div className="flex-1 min-w-0">
                        <p className="text-xs font-medium text-gray-800 truncate">
                          {it.summary || it.action || (it.kind === "inbox" ? "Draft" : "Task")}
                        </p>
                        <p className="text-[11px] text-gray-500 mt-0.5">
                          <span className="uppercase tracking-wide mr-1.5">{it.action || it.kind}</span>
                          · {formatRelative(it.timestamp)}
                        </p>
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </div>

            {/* Spacer to push buttons to bottom */}
            <div className="flex-1" />

            {/* Action buttons */}
            <div className="flex gap-3 px-5 py-5 border-t border-gray-100">
              <button
                disabled={running || agent.isNpc}
                className={cn(
                  "flex-1 h-10 rounded-lg text-sm font-medium transition-colors",
                  "bg-[#534AB7] text-white hover:bg-[#534AB7]/90",
                  "disabled:opacity-50 disabled:cursor-not-allowed"
                )}
                onClick={handleRunAgent}
              >
                {running ? "Running…" : "Run Agent"}
              </button>
              <button
                className={cn(
                  "flex-1 h-10 rounded-lg text-sm font-medium transition-colors",
                  "border border-gray-200 bg-white text-gray-700 hover:bg-gray-50"
                )}
                onClick={handleViewLogs}
              >
                View Logs
              </button>
            </div>
          </>
        )}
      </div>
    </>
  );
}
