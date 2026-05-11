// ---------------------------------------------------------------------------
// ARIA Virtual Office Configuration
// Derives agent metadata from agent-config.ts — desk layout is the only
// office-specific data defined here.
// ---------------------------------------------------------------------------

import { AGENT_DEFS, type AgentDef } from "./agent-config";

export const TILE_SIZE = 32;
export const OFFICE_WIDTH = 32;  // tiles
export const OFFICE_HEIGHT = 16; // tiles
export const OFFICE_PIXEL_WIDTH = OFFICE_WIDTH * TILE_SIZE;   // 768
export const OFFICE_PIXEL_HEIGHT = OFFICE_HEIGHT * TILE_SIZE; // 512

// Mobile canvas: 2-column × 4-row grid (7 rooms + 1 empty cell)
export const MOBILE_OFFICE_WIDTH = 16;  // tiles (2 × 8-tile columns)
export const MOBILE_OFFICE_HEIGHT = 32; // tiles (4 × 8-tile rows)
export const MOBILE_PIXEL_WIDTH = MOBILE_OFFICE_WIDTH * TILE_SIZE;   // 512
export const MOBILE_PIXEL_HEIGHT = MOBILE_OFFICE_HEIGHT * TILE_SIZE; // 1024

// ---- Types ----------------------------------------------------------------

export type AgentStatus = "running" | "busy" | "idle" | "working";

export interface OfficeAgent {
  id: string;
  name: string;
  role: string;
  model?: AgentDef["model"];
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
  { id: "design-studio", name: "Design Studio",   x: 24, y: 0, width: 8, height: 8, floorColor: "#FFF0F5", wallColor: "#E0A0B8", labelColor: "#E4407B" },
];

// ---- Mobile room layout (2-column × 4-row grid, 7 rooms + 1 empty cell) ---
// Col 0 = x:0, Col 1 = x:8. Rows at y: 0, 8, 16, 24. Bottom-right cell empty.

export const MOBILE_ROOMS: Room[] = [
  { id: "ceo-office",     name: "CEO Office",      x: 0, y:  0, width: 8, height: 8, floorColor: "#F5F0FF", wallColor: "#B8A8E0", labelColor: "#534AB7" },
  { id: "meeting-room",   name: "Meeting Room",    x: 8, y:  0, width: 8, height: 8, floorColor: "#FFF9F0", wallColor: "#D4C4A0", labelColor: "#8B7355" },
  { id: "content-studio", name: "Content Studio",  x: 0, y:  8, width: 8, height: 8, floorColor: "#EDFAF2", wallColor: "#A8D8B8", labelColor: "#1D9E75" },
  { id: "email-room",     name: "Email Center",    x: 8, y:  8, width: 8, height: 8, floorColor: "#FFF8ED", wallColor: "#E0C8A0", labelColor: "#BA7517" },
  { id: "social-hub",     name: "Social Hub",      x: 0, y: 16, width: 8, height: 8, floorColor: "#FFEFED", wallColor: "#E0A8A0", labelColor: "#D85A30" },
  { id: "ads-room",       name: "Ads Room",        x: 8, y: 16, width: 8, height: 8, floorColor: "#F0EDFF", wallColor: "#B8A0E0", labelColor: "#7C3AED" },
  { id: "design-studio",  name: "Design Studio",   x: 0, y: 24, width: 8, height: 8, floorColor: "#FFF0F5", wallColor: "#E0A0B8", labelColor: "#E4407B" },
  // Bottom-right cell (x:8, y:24) intentionally empty
];

/** Return MOBILE_ROOMS or ROOMS depending on viewport. */
export function getRoomsForViewport(isMobile: boolean): Room[] {
  return isMobile ? MOBILE_ROOMS : ROOMS;
}

// ---- Meeting center / chairs (desktop) ------------------------------------

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
  // Extra chair for 6th agent
  { x: TABLE_X + 32, y: TABLE_Y + 37 },
];

// ---- Meeting center / chairs (mobile) -------------------------------------
// Mobile meeting-room is at tile (8, 0) — identical to the desktop layout.
// Center = (8+4, 0+4) = (12, 4). TABLE_X/Y are the same as desktop.

export const MOBILE_MEETING_CENTER = { x: 12, y: 4 };

// Same table origin as desktop: meeting-room at (8,0) → TABLE_X=9*T+16=304, TABLE_Y=2*T+16=80
const MOBILE_TABLE_X = TABLE_X; // 304 — identical to desktop
const MOBILE_TABLE_Y = TABLE_Y; // 80  — identical to desktop

export const MOBILE_MEETING_CHAIRS: { x: number; y: number }[] = [
  { x: MOBILE_TABLE_X + 16, y: MOBILE_TABLE_Y - 5 },
  { x: MOBILE_TABLE_X + 48, y: MOBILE_TABLE_Y - 5 },
  { x: MOBILE_TABLE_X + 16, y: MOBILE_TABLE_Y + 37 },
  { x: MOBILE_TABLE_X + 48, y: MOBILE_TABLE_Y + 37 },
  { x: MOBILE_TABLE_X - 5,  y: MOBILE_TABLE_Y + 14 },
  { x: MOBILE_TABLE_X + 69, y: MOBILE_TABLE_Y + 14 },
  { x: MOBILE_TABLE_X + 32, y: MOBILE_TABLE_Y + 37 },
];

/** Return the appropriate meeting chairs depending on viewport. */
export function getMeetingChairsForViewport(isMobile: boolean): { x: number; y: number }[] {
  return isMobile ? MOBILE_MEETING_CHAIRS : MEETING_CHAIRS;
}

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
  "design-studio": [
    // Near easel
    { x: 26 * TILE_SIZE, y: 2 * TILE_SIZE },
    // Near color palette display
    { x: 30 * TILE_SIZE, y: 2 * TILE_SIZE },
    // Near plant
    { x: 25 * TILE_SIZE, y: 6 * TILE_SIZE + 8 },
    // Near bookshelf
    { x: 30 * TILE_SIZE + 16, y: 6 * TILE_SIZE },
    // Rug center
    { x: 28 * TILE_SIZE, y: 4 * TILE_SIZE },
  ],
};

// ---- All spots (flat array for cross-room wandering) ----------------------
export const ALL_IDLE_SPOTS: { x: number; y: number; room: string }[] = Object.entries(IDLE_SPOTS).flatMap(
  ([room, spots]) => spots.map((s) => ({ ...s, room }))
);

// ---- Mobile idle spot translation ------------------------------------------
// Each desktop idle spot is expressed in absolute canvas pixels based on the
// desktop room's position. On mobile the rooms are at different tile offsets.
// We compute the delta between mobile and desktop room origins and translate.

function _roomOriginPx(rooms: Room[], roomId: string): { x: number; y: number } {
  const r = rooms.find((r) => r.id === roomId);
  return r ? { x: r.x * TILE_SIZE, y: r.y * TILE_SIZE } : { x: 0, y: 0 };
}

/**
 * Returns idle spots for a single room, translated to mobile coords when
 * isMobile=true. Spots are stored in desktop canvas pixels — on mobile we
 * shift them by (mobileOrigin − desktopOrigin) so their relative offset
 * within the room is preserved.
 */
export function getIdleSpotsForRoom(
  roomId: string,
  isMobile: boolean,
): { x: number; y: number }[] {
  const spots = IDLE_SPOTS[roomId];
  if (!spots) return [];
  if (!isMobile) return spots;

  const desktopOrigin = _roomOriginPx(ROOMS, roomId);
  const mobileOrigin = _roomOriginPx(MOBILE_ROOMS, roomId);
  const dx = mobileOrigin.x - desktopOrigin.x;
  const dy = mobileOrigin.y - desktopOrigin.y;
  return spots.map((s) => ({ x: s.x + dx, y: s.y + dy }));
}

/**
 * Returns ALL_IDLE_SPOTS translated for the current viewport.
 */
export function getAllIdleSpotsForViewport(
  isMobile: boolean,
): { x: number; y: number; room: string }[] {
  if (!isMobile) return ALL_IDLE_SPOTS;
  return Object.keys(IDLE_SPOTS).flatMap((roomId) =>
    getIdleSpotsForRoom(roomId, true).map((s) => ({ ...s, room: roomId })),
  );
}

// Cross-room visit chance: 20% of wanders go to a random other room
export const CROSS_ROOM_CHANCE = 0.2;

// ---- Desk layout (office-specific data) -----------------------------------

const DESK_LAYOUT: Record<string, { department: string; deskX: number; deskY: number; hasCrown: boolean }> = {
  ceo:            { department: "ceo-office",     deskX: 3,  deskY: 4,  hasCrown: true },
  content_writer: { department: "content-studio", deskX: 20, deskY: 4,  hasCrown: false },
  email_marketer: { department: "email-room",     deskX: 3,  deskY: 12, hasCrown: false },
  social_manager: { department: "social-hub",     deskX: 12, deskY: 12, hasCrown: false },
  ad_strategist:  { department: "ads-room",       deskX: 20, deskY: 12, hasCrown: false },
  media:          { department: "design-studio", deskX: 28, deskY: 4,  hasCrown: false },
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
  { id: "npc_exec_asst",    name: "Alex",    role: "Executive Assistant",  color: "#8E7CC3", department: "ceo-office",     deskX: 5, deskY: 2, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Meeting Room
  { id: "npc_office_mgr",   name: "Jordan",  role: "Office Manager",       color: "#A0845C", department: "meeting-room",   deskX: 14, deskY: 6, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Content Studio
  { id: "npc_jr_writer",    name: "Sam",     role: "Junior Writer",        color: "#45B39D", department: "content-studio", deskX: 18, deskY: 3, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_designer",     name: "Riley",   role: "Graphic Designer",     color: "#EC7063", department: "content-studio", deskX: 22, deskY: 6, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Email Center
  { id: "npc_analyst",      name: "Morgan",  role: "Campaign Analyst",     color: "#F5B041", department: "email-room",     deskX: 5, deskY: 10, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_copywriter",   name: "Casey",   role: "Copywriter",           color: "#D4AC0D", department: "email-room",     deskX: 6, deskY: 14, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Social Hub
  { id: "npc_community",    name: "Taylor",  role: "Community Manager",    color: "#E67E22", department: "social-hub",     deskX: 10, deskY: 10, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_video",        name: "Avery",   role: "Video Editor",         color: "#CB4335", department: "social-hub",     deskX: 14, deskY: 14, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Ads Room
  { id: "npc_media_buyer",  name: "Quinn",   role: "Media Buyer",          color: "#7D3C98", department: "ads-room",       deskX: 18, deskY: 10, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_data",         name: "Drew",    role: "Data Analyst",         color: "#2E86C1", department: "ads-room",       deskX: 22, deskY: 14, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  // Design Studio
  { id: "npc_illustrator",  name: "Jamie",   role: "Illustrator",          color: "#E91E63", department: "design-studio",  deskX: 26, deskY: 2, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
  { id: "npc_ux_designer",  name: "Sage",    role: "UX Designer",          color: "#FF7043", department: "design-studio",  deskX: 30, deskY: 6, hasCrown: false, isNpc: true, status: "idle", currentTask: "", lastUpdated: "" },
];

export const AGENTS: OfficeAgent[] = [...CORE_AGENTS, ...NPC_STAFF];

// ---- Lookup helpers -------------------------------------------------------

export const ROOM_MAP: Record<string, Room> = Object.fromEntries(
  ROOMS.map((r) => [r.id, r]),
);

export const MOBILE_ROOM_MAP: Record<string, Room> = Object.fromEntries(
  MOBILE_ROOMS.map((r) => [r.id, r]),
);

/**
 * Convert an agent's desktop desk tile coords (deskX, deskY) to pixel coords
 * for the active viewport. On desktop, just multiply by TILE_SIZE. On mobile,
 * shift by the room origin delta so the desk stays in the same relative
 * position within its room.
 */
export function getDeskPixel(
  deskX: number,
  deskY: number,
  department: string,
  isMobile: boolean,
): { x: number; y: number } {
  if (!isMobile) {
    return { x: deskX * TILE_SIZE + TILE_SIZE / 2, y: deskY * TILE_SIZE + TILE_SIZE / 2 };
  }
  const desktopRoom = ROOM_MAP[department];
  const mobileRoom = MOBILE_ROOM_MAP[department];
  const dx = desktopRoom ? desktopRoom.x : 0;
  const dy = desktopRoom ? desktopRoom.y : 0;
  const mx = mobileRoom ? mobileRoom.x : 0;
  const my = mobileRoom ? mobileRoom.y : 0;
  const localX = deskX - dx; // tile offset within the room
  const localY = deskY - dy;
  return {
    x: (mx + localX) * TILE_SIZE + TILE_SIZE / 2,
    y: (my + localY) * TILE_SIZE + TILE_SIZE / 2,
  };
}

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
