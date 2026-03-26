// ---------------------------------------------------------------------------
// ARIA Virtual Office Configuration
// Derives agent metadata from agent-config.ts — desk layout is the only
// office-specific data defined here.
// ---------------------------------------------------------------------------

import { AGENT_DEFS, type AgentDef } from "./agent-config";

export const TILE_SIZE = 32;
export const OFFICE_WIDTH = 24;  // tiles
export const OFFICE_HEIGHT = 16; // tiles
export const OFFICE_PIXEL_WIDTH = OFFICE_WIDTH * TILE_SIZE;   // 768
export const OFFICE_PIXEL_HEIGHT = OFFICE_HEIGHT * TILE_SIZE; // 512

// ---- Types ----------------------------------------------------------------

export type AgentStatus = "running" | "busy" | "idle" | "working";

export interface OfficeAgent {
  id: string;
  name: string;
  role: string;
  model: AgentDef["model"];
  status: AgentStatus;
  department: string;
  currentTask: string;
  lastUpdated: string;
  deskX: number;
  deskY: number;
  color: string;
  hasCrown: boolean;
}

export interface Room {
  id: string;
  name: string;
  x: number;
  y: number;
  width: number;
  height: number;
  floorColor: string;
  wallColor: string;
  labelColor: string;
}

// ---- Rooms ----------------------------------------------------------------

export const ROOMS: Room[] = [
  { id: "ceo-office",     name: "CEO Office",      x: 0,  y: 0, width: 8, height: 8, floorColor: "#F5F0FF", wallColor: "#B8A8E0", labelColor: "#534AB7" },
  { id: "meeting-room",   name: "Meeting Room",    x: 8,  y: 0, width: 8, height: 8, floorColor: "#FFF9F0", wallColor: "#D4C4A0", labelColor: "#8B7355" },
  { id: "content-studio", name: "Content Studio",  x: 16, y: 0, width: 8, height: 8, floorColor: "#EDFAF2", wallColor: "#A8D8B8", labelColor: "#1D9E75" },
  { id: "email-room",     name: "Email Center",    x: 0,  y: 8, width: 8, height: 8, floorColor: "#FFF8ED", wallColor: "#E0C8A0", labelColor: "#BA7517" },
  { id: "social-hub",     name: "Social Hub",      x: 8,  y: 8, width: 8, height: 8, floorColor: "#FFEFED", wallColor: "#E0A8A0", labelColor: "#D85A30" },
  { id: "ads-room",       name: "Ads Room",        x: 16, y: 8, width: 8, height: 8, floorColor: "#F0EDFF", wallColor: "#B8A0E0", labelColor: "#7C3AED" },
];

export const MEETING_CENTER = { x: 12, y: 4 };

// ---- Desk layout (office-specific data) -----------------------------------

const DESK_LAYOUT: Record<string, { department: string; deskX: number; deskY: number; hasCrown: boolean }> = {
  ceo:            { department: "ceo-office",     deskX: 3,  deskY: 4,  hasCrown: true },
  content_writer: { department: "content-studio", deskX: 20, deskY: 4,  hasCrown: false },
  email_marketer: { department: "email-room",     deskX: 3,  deskY: 12, hasCrown: false },
  social_manager: { department: "social-hub",     deskX: 12, deskY: 12, hasCrown: false },
  ad_strategist:  { department: "ads-room",       deskX: 20, deskY: 12, hasCrown: false },
};

// ---- Agents (derived from agent-config + desk layout) ---------------------

export const AGENTS: OfficeAgent[] = AGENT_DEFS.map((def) => {
  const desk = DESK_LAYOUT[def.slug];
  return {
    id: def.slug,
    name: def.name,
    role: def.role,
    model: def.model,
    color: def.color,
    status: "idle" as AgentStatus,
    currentTask: "",
    lastUpdated: new Date().toISOString(),
    ...desk,
  };
});

// ---- Lookup helpers -------------------------------------------------------

export const ROOM_MAP: Record<string, Room> = Object.fromEntries(
  ROOMS.map((r) => [r.id, r]),
);

export const AGENT_MAP: Record<string, OfficeAgent> = Object.fromEntries(
  AGENTS.map((a) => [a.id, a]),
);

export function getAgentsByDepartment(deptId: string): OfficeAgent[] {
  return AGENTS.filter((a) => a.department === deptId);
}

export function getRoomForAgent(agentId: string): Room | undefined {
  const a = AGENT_MAP[agentId];
  return a ? ROOM_MAP[a.department] : undefined;
}
