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
  isNpc?: boolean; // visual-only office staff (don't attend meetings, can't be delegated to)
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

// ---- Meeting chair positions (pixel coords around the conference table) ----
// The conference table is drawn at (9*T+16, 2*T+16) = (304, 80), size 64x32.
// Chairs are drawn at offsets from the table origin (tX=304, tY=80).
const TABLE_X = 9 * TILE_SIZE + 16; // 304
const TABLE_Y = 2 * TILE_SIZE + 16; // 80

export const MEETING_CHAIRS: { x: number; y: number }[] = [
  // Top chairs (head of table = index 0 for CEO)
  { x: TABLE_X + 16, y: TABLE_Y - 5 },
  { x: TABLE_X + 48, y: TABLE_Y - 5 },
  // Bottom chairs
  { x: TABLE_X + 16, y: TABLE_Y + 37 },
  { x: TABLE_X + 48, y: TABLE_Y + 37 },
  // Side chairs
  { x: TABLE_X - 5,  y: TABLE_Y + 14 },
  { x: TABLE_X + 69, y: TABLE_Y + 14 },
];

// ---- Idle wander spots per department (pixel coords) -----------------------
// Each room is 8x8 tiles. Spots are near decorations but not on top of them.
// T=32, so tile (tx,ty) -> pixel center = (tx*32+16, ty*32+16).

export const IDLE_SPOTS: Record<string, { x: number; y: number }[]> = {
  "ceo-office": [
    // Near bookshelf (1*T+16, 1*T+20)
    { x: 1 * TILE_SIZE + 28, y: 2 * TILE_SIZE + 16 },
    // Near lamp (6*T+16, 1*T+16)
    { x: 6 * TILE_SIZE + 16, y: 2 * TILE_SIZE + 16 },
    // Near plant (6*T+16, 6*T+16)
    { x: 5 * TILE_SIZE + 16, y: 6 * TILE_SIZE + 8 },
    // Rug center
    { x: 4 * TILE_SIZE, y: 4 * TILE_SIZE },
    // Near door (right side of room)
    { x: 7 * TILE_SIZE, y: 6 * TILE_SIZE },
  ],
  "meeting-room": [
    // Near whiteboard (9*T+4, 0*T+20)
    { x: 10 * TILE_SIZE, y: 1 * TILE_SIZE + 16 },
    // Near plant right (15*T, 1*T+16)
    { x: 14 * TILE_SIZE + 16, y: 2 * TILE_SIZE },
    // Near plant bottom (9*T+16, 6*T+16)
    { x: 10 * TILE_SIZE, y: 6 * TILE_SIZE + 8 },
    // Near door (bottom of room)
    { x: 12 * TILE_SIZE, y: 7 * TILE_SIZE },
  ],
  "content-studio": [
    // Near bookshelf (22*T, 1*T+4)
    { x: 22 * TILE_SIZE + 12, y: 2 * TILE_SIZE + 16 },
    // Near whiteboard (17*T+4, 1*T+4)
    { x: 18 * TILE_SIZE, y: 2 * TILE_SIZE },
    // Near lamp (22*T+16, 6*T)
    { x: 22 * TILE_SIZE, y: 5 * TILE_SIZE + 16 },
    // Near plant (17*T+16, 6*T+16)
    { x: 18 * TILE_SIZE, y: 6 * TILE_SIZE + 8 },
    // Rug center
    { x: 20 * TILE_SIZE, y: 4 * TILE_SIZE },
  ],
  "email-room": [
    // Near cabinet left (1*T+4, 9*T+4)
    { x: 2 * TILE_SIZE, y: 10 * TILE_SIZE },
    // Near cabinet right (1*T+20, 9*T+4)
    { x: 2 * TILE_SIZE + 16, y: 10 * TILE_SIZE },
    // Near coffee machine (5*T+8, 9*T+4)
    { x: 5 * TILE_SIZE + 16, y: 10 * TILE_SIZE },
    // Near plant (6*T+16, 14*T+16)
    { x: 5 * TILE_SIZE + 16, y: 14 * TILE_SIZE + 8 },
    // Rug center
    { x: 4 * TILE_SIZE, y: 12 * TILE_SIZE },
  ],
  "social-hub": [
    // Near whiteboard (9*T+4, 9*T+4)
    { x: 10 * TILE_SIZE, y: 10 * TILE_SIZE },
    // Near lamp (9*T+16, 14*T+16)
    { x: 10 * TILE_SIZE, y: 14 * TILE_SIZE + 8 },
    // Near plant (14*T+16, 14*T+16)
    { x: 14 * TILE_SIZE, y: 14 * TILE_SIZE + 8 },
    // Rug center
    { x: 12 * TILE_SIZE, y: 12 * TILE_SIZE },
    // Near door
    { x: 12 * TILE_SIZE, y: 15 * TILE_SIZE },
  ],
  "ads-room": [
    // Near bookshelf (17*T+4, 9*T+4)
    { x: 18 * TILE_SIZE, y: 10 * TILE_SIZE },
    // Near cabinet (22*T+8, 9*T+4)
    { x: 22 * TILE_SIZE + 16, y: 10 * TILE_SIZE },
    // Near water cooler (22*T+16, 14*T+8)
    { x: 22 * TILE_SIZE, y: 14 * TILE_SIZE },
    // Near plant (17*T+16, 14*T+16)
    { x: 18 * TILE_SIZE, y: 14 * TILE_SIZE + 8 },
    // Rug center
    { x: 20 * TILE_SIZE, y: 12 * TILE_SIZE },
  ],
};

// ---- Desk layout (office-specific data) -----------------------------------

const DESK_LAYOUT: Record<string, { department: string; deskX: number; deskY: number; hasCrown: boolean }> = {
  ceo:            { department: "ceo-office",     deskX: 3,  deskY: 4,  hasCrown: true },
  content_writer: { department: "content-studio", deskX: 20, deskY: 4,  hasCrown: false },
  email_marketer: { department: "email-room",     deskX: 3,  deskY: 12, hasCrown: false },
  social_manager: { department: "social-hub",     deskX: 12, deskY: 12, hasCrown: false },
  ad_strategist:  { department: "ads-room",       deskX: 20, deskY: 12, hasCrown: false },
};

// ---- Core agents (derived from agent-config + desk layout) ----------------

const CORE_AGENTS: OfficeAgent[] = AGENT_DEFS.map((def) => {
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

// ---- NPC office staff (visual only — wander, don't attend meetings) -------

const NPC_STAFF: OfficeAgent[] = [
  // CEO Office
  { id: "npc_exec_asst",    name: "Alex",    role: "Executive Assistant",  model: "haiku-4-5", color: "#8E7CC3", department: "ceo-office",     deskX: 5, deskY: 2, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Meeting Room
  { id: "npc_office_mgr",   name: "Jordan",  role: "Office Manager",       model: "haiku-4-5", color: "#A0845C", department: "meeting-room",   deskX: 14, deskY: 6, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Content Studio
  { id: "npc_jr_writer",    name: "Sam",     role: "Junior Writer",        model: "haiku-4-5", color: "#45B39D", department: "content-studio", deskX: 18, deskY: 3, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_designer",     name: "Riley",   role: "Graphic Designer",     model: "haiku-4-5", color: "#EC7063", department: "content-studio", deskX: 22, deskY: 6, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Email Center
  { id: "npc_analyst",      name: "Morgan",  role: "Campaign Analyst",     model: "haiku-4-5", color: "#F5B041", department: "email-room",     deskX: 5, deskY: 10, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_copywriter",   name: "Casey",   role: "Copywriter",           model: "haiku-4-5", color: "#D4AC0D", department: "email-room",     deskX: 6, deskY: 14, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Social Hub
  { id: "npc_community",    name: "Taylor",  role: "Community Manager",    model: "haiku-4-5", color: "#E67E22", department: "social-hub",     deskX: 10, deskY: 10, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_video",        name: "Avery",   role: "Video Editor",         model: "haiku-4-5", color: "#CB4335", department: "social-hub",     deskX: 14, deskY: 14, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Ads Room
  { id: "npc_media_buyer",  name: "Quinn",   role: "Media Buyer",          model: "haiku-4-5", color: "#7D3C98", department: "ads-room",       deskX: 18, deskY: 10, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_data",         name: "Drew",    role: "Data Analyst",         model: "haiku-4-5", color: "#2E86C1", department: "ads-room",       deskX: 22, deskY: 14, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
];

export const AGENTS: OfficeAgent[] = [...CORE_AGENTS, ...NPC_STAFF];

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
