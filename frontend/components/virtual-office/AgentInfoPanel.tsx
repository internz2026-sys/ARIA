"use client";

import { useEffect, useRef } from "react";
import { cn } from "@/lib/utils";
import type { OfficeAgent, AgentStatus } from "@/lib/office-config";

interface AgentInfoPanelProps {
  agent: OfficeAgent | null;
  onClose: () => void;
}

const MODEL_DISPLAY: Record<
  AgentInfoPanelProps["agent"] & object extends infer A
    ? A extends { model: infer M } ? M : never
    : never,
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
          "fixed top-0 right-0 z-50 h-full w-[360px] bg-white border-l border-gray-200 shadow-xl",
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
                <span className="text-sm text-gray-700">{agent.lastUpdated}</span>
              </div>
            </div>

            {/* Divider */}
            <div className="mx-5 border-t border-gray-100" />

            {/* Recent Activity */}
            <div className="flex flex-col gap-3 px-5 py-5">
              <h3 className="text-sm font-semibold text-gray-900">Recent Activity</h3>
              <div className="flex items-center justify-center py-6 text-sm text-gray-400 bg-gray-50 rounded-lg">
                No recent activity
              </div>
            </div>

            {/* Spacer to push buttons to bottom */}
            <div className="flex-1" />

            {/* Action buttons */}
            <div className="flex gap-3 px-5 py-5 border-t border-gray-100">
              <button
                className={cn(
                  "flex-1 h-10 rounded-lg text-sm font-medium transition-colors",
                  "bg-[#534AB7] text-white hover:bg-[#534AB7]/90"
                )}
                onClick={() => {}}
              >
                Run Agent
              </button>
              <button
                className={cn(
                  "flex-1 h-10 rounded-lg text-sm font-medium transition-colors",
                  "border border-gray-200 bg-white text-gray-700 hover:bg-gray-50"
                )}
                onClick={() => {}}
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
