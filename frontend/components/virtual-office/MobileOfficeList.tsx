"use client";

import { ROOMS } from "@/lib/office-config";
import type { OfficeAgent } from "@/lib/office-config";

interface MobileOfficeListProps {
  agents: OfficeAgent[];
  onAgentClick: (agentId: string) => void;
}

const STATUS_COLORS: Record<string, string> = {
  idle: "#1D9E75",
  running: "#534AB7",
  working: "#3B82F6",
  busy: "#EAB308",
};

const STATUS_LABELS: Record<string, string> = {
  idle: "Idle",
  running: "In Meeting",
  working: "Working",
  busy: "Busy",
};

const MODEL_LABELS: Record<string, string> = {
  "opus-4-6": "Opus 4.6",
  "sonnet-4-6": "Sonnet 4.6",
  "haiku-4-5": "Haiku 4.5",
};

export default function MobileOfficeList({ agents, onAgentClick }: MobileOfficeListProps) {
  return (
    <div className="flex flex-col gap-3 p-3">
      {ROOMS.map((room) => {
        const roomAgents = agents.filter(
          (a) => a.department === room.id && !a.isNpc,
        );
        const npcAgents = agents.filter(
          (a) => a.department === room.id && a.isNpc,
        );

        return (
          <div
            key={room.id}
            className="rounded-xl border border-[#E0DED8] bg-white overflow-hidden"
            style={{ borderLeftWidth: 4, borderLeftColor: room.labelColor }}
          >
            {/* Room header */}
            <div className="flex items-center gap-2 px-4 pt-3 pb-2">
              <span
                className="w-2.5 h-2.5 rounded-full shrink-0"
                style={{ backgroundColor: room.labelColor }}
              />
              <span className="text-sm font-bold text-[#2C2C2A]">{room.name}</span>
              <span className="ml-auto text-[10px] text-[#9B9A96]">
                {roomAgents.length + npcAgents.length} people
              </span>
            </div>

            {/* Core agents */}
            {roomAgents.length > 0 && (
              <div className="px-4 pb-2 flex flex-col gap-2">
                {roomAgents.map((agent) => (
                  <button
                    key={agent.id}
                    className="flex items-center gap-3 w-full text-left rounded-lg py-2 px-1 -mx-1 active:bg-[#F5F4F2] transition-colors"
                    onClick={() => onAgentClick(agent.id)}
                  >
                    {/* Avatar */}
                    <div
                      className="w-9 h-9 rounded-full flex items-center justify-center text-white text-sm font-bold shrink-0"
                      style={{ backgroundColor: agent.color }}
                    >
                      {agent.hasCrown ? (
                        <span className="text-[10px] leading-none">♛</span>
                      ) : (
                        agent.name.charAt(0)
                      )}
                    </div>

                    {/* Name + role */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-sm font-semibold text-[#2C2C2A] truncate">
                          {agent.name}
                        </span>
                        {agent.hasCrown && (
                          <span className="text-[10px] text-[#FFD700]">♛</span>
                        )}
                      </div>
                      <div className="flex items-center gap-1.5 mt-0.5">
                        <span className="text-[11px] text-[#5F5E5A] truncate">
                          {agent.role}
                        </span>
                        {agent.model && (
                          <span
                            className="shrink-0 text-[9px] font-medium px-1.5 py-0.5 rounded-full text-white"
                            style={{ backgroundColor: agent.color + "BB" }}
                          >
                            {MODEL_LABELS[agent.model] ?? agent.model}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Status dot + label */}
                    <div className="flex items-center gap-1 shrink-0">
                      <span
                        className="w-2 h-2 rounded-full"
                        style={{
                          backgroundColor:
                            STATUS_COLORS[agent.status] ?? "#1D9E75",
                        }}
                      />
                      <span className="text-[10px] text-[#5F5E5A]">
                        {STATUS_LABELS[agent.status] ?? agent.status}
                      </span>
                    </div>
                  </button>
                ))}
              </div>
            )}

            {/* NPC staff — smaller, non-interactive */}
            {npcAgents.length > 0 && (
              <div className="px-4 pb-3 border-t border-[#F0EEE8] mt-1 pt-2 flex flex-wrap gap-x-3 gap-y-1">
                {npcAgents.map((npc) => (
                  <span key={npc.id} className="flex items-center gap-1">
                    <span
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ backgroundColor: npc.color }}
                    />
                    <span className="text-[10px] text-[#9B9A96]">
                      {npc.name}
                    </span>
                  </span>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
