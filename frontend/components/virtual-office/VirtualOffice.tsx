"use client";

import React, { useRef, useEffect, useCallback, useState } from "react";
import {
  TILE_SIZE,
  OFFICE_PIXEL_WIDTH,
  OFFICE_PIXEL_HEIGHT,
  ROOMS,
  AGENTS as DEFAULT_AGENTS,
  MEETING_CHAIRS,
  IDLE_SPOTS,
  type OfficeAgent,
  type Room,
} from "@/lib/office-config";

interface VirtualOfficeProps {
  agents: OfficeAgent[];
  onAgentClick: (agentId: string) => void;
}

// Track animated positions per agent
interface AnimPos {
  x: number;
  y: number;
  targetX: number;
  targetY: number;
  walking: boolean;
  facingRight: boolean;
  walkFrame: number;
  // Idle wandering
  idleTimer: number;       // countdown frames until next wander
  wanderTarget: boolean;   // true = heading to idle spot, false = heading home to desk
  wanderPause: number;     // pause frames when arrived at idle spot
  thoughtIcon: number;     // index into thought icons (cycles)
  thoughtTimer: number;    // countdown to next thought change
  waveTimer: number;       // > 0 means wave animation is playing
}

const T = TILE_SIZE;
const WALK_SPEED = 1.75; // pixels per frame — walking to meeting / back to desk (5x faster)
const STROLL_SPEED = 0.36; // pixels per frame — idle wander around office (2x faster)

// Thought bubble icons (drawn as simple shapes/text, no emoji)
const THOUGHT_ICONS = ["?", "!", "~", "*", "#"];
const THOUGHT_LABELS = ["hmm", "idea", "coffee", "note", "plan"];

export default function VirtualOffice({ agents, onAgentClick }: VirtualOfficeProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<number>(0);
  const [hoveredAgent, setHoveredAgent] = useState<string | null>(null);
  const scaleRef = useRef(1);
  const agentList = agents.length > 0 ? agents : DEFAULT_AGENTS;

  // Animated positions — persistent across frames
  const posRef = useRef<Record<string, AnimPos>>({});
  // Track previous status per agent to detect changes
  const prevStatusRef = useRef<Record<string, string>>({});

  // Helper: compute target position for a given status
  const getTarget = useCallback((agent: OfficeAgent, idx: number) => {
    const deskPx = { x: agent.deskX * T + T / 2, y: agent.deskY * T + T / 2 };
    if (agent.status === "running") {
      // CEO always gets chair 0 (head of table)
      const chairIdx = agent.id === "ceo" ? 0 : Math.min(idx, MEETING_CHAIRS.length - 1);
      const chair = MEETING_CHAIRS[chairIdx];
      return { x: chair.x, y: chair.y };
    }
    return deskPx; // working, idle, busy — all at desk
  }, []);

  // Initialize / update targets when agent status changes
  useEffect(() => {
    const pos = posRef.current;
    const prevStatus = prevStatusRef.current;
    for (let idx = 0; idx < agentList.length; idx++) {
      const agent = agentList[idx];
      const target = getTarget(agent, idx);

      if (!pos[agent.id]) {
        // First time: start at the TARGET position (not desk) so agents
        // appear where they should be based on their persisted status.
        // This prevents "reset to desk" when navigating away and back.
        pos[agent.id] = {
          x: target.x, y: target.y,
          targetX: target.x, targetY: target.y,
          walking: false, facingRight: true, walkFrame: 0,
          idleTimer: Math.floor(300 + Math.random() * 500),
          wanderTarget: false,
          wanderPause: 0,
          thoughtIcon: Math.floor(Math.random() * THOUGHT_ICONS.length),
          thoughtTimer: Math.floor(180 + Math.random() * 240),
          waveTimer: 0,
        };
        prevStatus[agent.id] = agent.status;
        continue;
      }

      // Skip if status hasn't changed
      if (prevStatus[agent.id] === agent.status) continue;
      prevStatus[agent.id] = agent.status;

      const p = pos[agent.id];
      p.targetX = target.x;
      p.targetY = target.y;
      // Reset idle wander state when status changes
      p.wanderTarget = false;
      p.wanderPause = 0;
      p.idleTimer = Math.floor(300 + Math.random() * 500);
    }
  }, [agentList, getTarget]);

  // Resize — canvas is absolutely positioned so it can't inflate the wrapper
  const resize = useCallback(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const rect = wrap.getBoundingClientRect();
    if (rect.width < 10 || rect.height < 10) return;
    const scaleX = rect.width / OFFICE_PIXEL_WIDTH;
    const scaleY = rect.height / OFFICE_PIXEL_HEIGHT;
    const scale = Math.min(scaleX, scaleY);
    scaleRef.current = scale;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.floor(OFFICE_PIXEL_WIDTH * scale);
    const h = Math.floor(OFFICE_PIXEL_HEIGHT * scale);
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    // Center canvas within wrapper via absolute positioning
    canvas.style.left = `${Math.max(0, Math.floor((rect.width - w) / 2))}px`;
    canvas.style.top = `${Math.max(0, Math.floor((rect.height - h) / 2))}px`;
    const ctx = canvas.getContext("2d");
    if (ctx) ctx.setTransform(scale * dpr, 0, 0, scale * dpr, 0, 0);
  }, []);

  // ── Drawing helpers ──────────────────────────────────────────────────────

  const drawRoom = useCallback((ctx: CanvasRenderingContext2D, room: Room) => {
    const x = room.x * T, y = room.y * T, w = room.width * T, h = room.height * T;
    // Floor
    ctx.fillStyle = room.floorColor;
    ctx.fillRect(x, y, w, h);
    // Grid
    ctx.strokeStyle = "rgba(0,0,0,0.04)";
    ctx.lineWidth = 0.5;
    for (let tx = room.x; tx <= room.x + room.width; tx++) {
      ctx.beginPath(); ctx.moveTo(tx * T, y); ctx.lineTo(tx * T, y + h); ctx.stroke();
    }
    for (let ty = room.y; ty <= room.y + room.height; ty++) {
      ctx.beginPath(); ctx.moveTo(x, ty * T); ctx.lineTo(x + w, ty * T); ctx.stroke();
    }
    // Walls
    ctx.strokeStyle = room.wallColor;
    ctx.lineWidth = 3;
    ctx.strokeRect(x + 1.5, y + 1.5, w - 3, h - 3);
    // Label
    ctx.fillStyle = room.labelColor;
    ctx.font = "bold 10px 'Courier New', monospace";
    ctx.textAlign = "center";
    ctx.fillText(room.name, x + w / 2, y + 14);
    ctx.textAlign = "left";
  }, []);

  const drawDesk = useCallback((ctx: CanvasRenderingContext2D, px: number, py: number) => {
    // Desk surface
    ctx.fillStyle = "#C4A67D";
    ctx.fillRect(px - 12, py - 4, 24, 12);
    ctx.fillStyle = "#A88B6A";
    ctx.fillRect(px - 12, py + 6, 24, 2);
    // Monitor
    ctx.fillStyle = "#2C2C2A";
    ctx.fillRect(px - 6, py - 12, 12, 9);
    ctx.fillStyle = "#8BA4D0";
    ctx.fillRect(px - 5, py - 11, 10, 7);
    // Stand
    ctx.fillStyle = "#555";
    ctx.fillRect(px - 2, py - 3, 4, 2);
  }, []);

  const drawAgent = useCallback((
    ctx: CanvasRenderingContext2D,
    agent: OfficeAgent,
    time: number,
    isHovered: boolean,
    px: number,
    py: number,
    walking: boolean,
    walkFrame: number,
    facingRight: boolean,
  ) => {
    const cx = px;
    const baseY = py + 6;
    // Breathing bob
    let bobY = 0;
    if (!walking) {
      bobY = (agent.status === "running" || agent.status === "working")
        ? Math.sin(time * 4) * 2.5
        : Math.sin(time * 1.5) * 0.6;
    } else {
      // Bounce while walking
      bobY = Math.abs(Math.sin(walkFrame * 0.35)) * -3;
    }
    const y = baseY + bobY;

    // Walk animation — legs
    const legOffset = walking ? Math.sin(walkFrame * 0.3) * 5 : 0;

    // Subtle working glow (at desk, executing a task)
    if (agent.status === "working" && !walking) {
      const pulse = 0.2 + Math.sin(time * 2.5) * 0.1;
      ctx.beginPath(); ctx.arc(cx, y - 8, 14, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(59,130,246,${pulse})`; ctx.fill();
    }

    // Hover ring
    if (isHovered) {
      ctx.beginPath(); ctx.arc(cx, y - 8, 20, 0, Math.PI * 2);
      ctx.strokeStyle = "#534AB7"; ctx.lineWidth = 2.5; ctx.stroke();
    }

    // Shadow — larger when walking
    const shadowSize = walking ? 10 : 8;
    ctx.fillStyle = walking ? "rgba(0,0,0,0.12)" : "rgba(0,0,0,0.08)";
    ctx.beginPath(); ctx.ellipse(cx, baseY + 10, shadowSize, 3.5, 0, 0, Math.PI * 2); ctx.fill();

    // Legs — thicker when walking
    ctx.fillStyle = "#5C5C5C";
    const legW = walking ? 3 : 2;
    ctx.fillRect(cx - 3, y, legW, 6 + legOffset);
    ctx.fillRect(cx + 1, y, legW, 6 - legOffset);

    // Body
    ctx.fillStyle = agent.color;
    ctx.fillRect(cx - 6, y - 12, 12, 12);
    // Body highlight
    ctx.fillStyle = "rgba(255,255,255,0.15)";
    ctx.fillRect(cx - 5, y - 11, 4, 10);

    // Arms — with wave animation on hover
    const p = posRef.current[agent.id];
    const waving = p && p.waveTimer > 0;
    if (walking) {
      const armSwing = Math.sin(walkFrame * 0.3) * 5;
      ctx.fillStyle = agent.color;
      ctx.fillRect(cx - 8, y - 10 + armSwing, 2, 8);
      ctx.fillRect(cx + 6, y - 10 - armSwing, 2, 8);
    } else if (waving) {
      // Wave! Right arm goes up
      const waveAngle = Math.sin(p.waveTimer * 0.4) * 4;
      ctx.fillStyle = agent.color;
      ctx.fillRect(cx - 8, y - 10, 2, 8);
      ctx.fillRect(cx + 6, y - 18 + waveAngle, 2, 8);
    } else if (agent.status === "working") {
      const armOff = Math.sin(time * 10) * 3.5;
      ctx.fillStyle = agent.color;
      ctx.fillRect(cx - 8, y - 10 + armOff, 2, 8);
      ctx.fillRect(cx + 6, y - 10 - armOff, 2, 8);
    } else if (agent.status === "running" && !walking) {
      // Sitting pose at meeting — arms on table
      ctx.fillStyle = agent.color;
      ctx.fillRect(cx - 8, y - 6, 2, 6);
      ctx.fillRect(cx + 6, y - 6, 2, 6);
    } else {
      ctx.fillStyle = agent.color;
      ctx.fillRect(cx - 8, y - 10, 2, 8);
      ctx.fillRect(cx + 6, y - 10, 2, 8);
    }

    // Head
    ctx.fillStyle = "#FDDCB5";
    ctx.beginPath(); ctx.arc(cx, y - 18, 7, 0, Math.PI * 2); ctx.fill();
    // Hair
    ctx.fillStyle = agent.color;
    ctx.beginPath(); ctx.arc(cx, y - 20, 7, Math.PI, Math.PI * 2); ctx.fill();
    // Eyes
    ctx.fillStyle = "#2C2C2A";
    const eyeDir = facingRight ? 1 : -1;
    ctx.fillRect(cx - 3 + eyeDir, y - 19, 2, 2);
    ctx.fillRect(cx + 1 + eyeDir, y - 19, 2, 2);
    // Mouth
    ctx.fillStyle = "#C4937A";
    ctx.fillRect(cx - 1, y - 15, 2, 1);

    // Crown
    if (agent.hasCrown) {
      const crownY = y - 28;
      ctx.fillStyle = "#FFD700";
      ctx.beginPath();
      ctx.moveTo(cx - 6, crownY + 5);
      ctx.lineTo(cx - 6, crownY + 1);
      ctx.lineTo(cx - 3, crownY + 3);
      ctx.lineTo(cx, crownY);
      ctx.lineTo(cx + 3, crownY + 3);
      ctx.lineTo(cx + 6, crownY + 1);
      ctx.lineTo(cx + 6, crownY + 5);
      ctx.closePath(); ctx.fill();
      // Jewels
      ctx.fillStyle = "#E74C3C"; ctx.beginPath(); ctx.arc(cx, crownY + 1, 1, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#3498DB"; ctx.beginPath(); ctx.arc(cx - 3, crownY + 3, 1, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#3498DB"; ctx.beginPath(); ctx.arc(cx + 3, crownY + 3, 1, 0, Math.PI * 2); ctx.fill();
    }

    // Status dot
    const dotColors: Record<string, string> = { idle: "#1D9E75", running: "#534AB7", working: "#3B82F6", busy: "#EAB308" };
    ctx.beginPath(); ctx.arc(cx + 9, y - 24, 3, 0, Math.PI * 2);
    ctx.fillStyle = dotColors[agent.status] || "#1D9E75"; ctx.fill();
    ctx.strokeStyle = "#fff"; ctx.lineWidth = 1; ctx.stroke();

    // Name label below feet — main agents get a bright highlight border
    const isMain = !agent.isNpc;
    ctx.font = isMain ? "bold 7px 'Courier New', monospace" : "bold 6px 'Courier New', monospace";
    const name = agent.name;
    const tw = ctx.measureText(name).width;
    const labelY = baseY + 14;
    if (isMain) {
      // Bright outline for core agents
      ctx.fillStyle = agent.color;
      ctx.beginPath(); ctx.roundRect(cx - tw / 2 - 4, labelY - 1, tw + 8, 12, 3); ctx.fill();
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.roundRect(cx - tw / 2 - 4, labelY - 1, tw + 8, 12, 3); ctx.stroke();
    } else {
      ctx.fillStyle = "rgba(44,44,42,0.6)";
      ctx.beginPath(); ctx.roundRect(cx - tw / 2 - 3, labelY, tw + 6, 10, 2); ctx.fill();
    }
    ctx.fillStyle = "#fff";
    ctx.textAlign = "center";
    ctx.fillText(name, cx, labelY + (isMain ? 8 : 7));
    ctx.textAlign = "left";
  }, []);

  // ── Decorations ──────────────────────────────────────────────────────────

  const drawDecorations = useCallback((ctx: CanvasRenderingContext2D, time: number) => {
    // Helper: plant
    function plant(x: number, y: number, s = 1) {
      ctx.fillStyle = "#A86040"; ctx.fillRect(x - 4 * s, y + 1 * s, 8 * s, 2 * s);
      ctx.fillStyle = "#C4785A"; ctx.fillRect(x - 3 * s, y + 2 * s, 6 * s, 5 * s);
      ctx.fillStyle = "#4CAF50"; ctx.beginPath(); ctx.arc(x, y - 2 * s, 5 * s, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#66BB6A"; ctx.beginPath(); ctx.arc(x - 3 * s, y, 3 * s, 0, Math.PI * 2); ctx.fill();
      ctx.beginPath(); ctx.arc(x + 3 * s, y, 3 * s, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#81C784"; ctx.beginPath(); ctx.arc(x, y - 5 * s, 3 * s, 0, Math.PI * 2); ctx.fill();
    }
    // Helper: bookshelf
    function bookshelf(x: number, y: number) {
      ctx.fillStyle = "#8B6F47"; ctx.fillRect(x, y, 24, 20);
      ctx.fillStyle = "#A0845C"; ctx.fillRect(x + 2, y + 2, 20, 7); ctx.fillRect(x + 2, y + 11, 20, 7);
      ctx.fillStyle = "#7A5C35"; ctx.fillRect(x + 1, y + 9, 22, 2);
      const cols = ["#E74C3C", "#3498DB", "#2ECC71", "#F39C12", "#9B59B6"];
      for (let i = 0; i < 5; i++) { ctx.fillStyle = cols[i]; ctx.fillRect(x + 3 + i * 4, y + 3, 3, 6); }
      for (let i = 0; i < 4; i++) { ctx.fillStyle = cols[(i + 2) % 5]; ctx.fillRect(x + 4 + i * 4, y + 12, 3, 5); }
    }
    // Helper: whiteboard
    function whiteboard(x: number, y: number) {
      ctx.fillStyle = "#888"; ctx.fillRect(x, y, 32, 20);
      ctx.fillStyle = "#F8F8F8"; ctx.fillRect(x + 2, y + 2, 28, 16);
      ctx.strokeStyle = "#D85A30"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x + 5, y + 6); ctx.lineTo(x + 22, y + 6); ctx.stroke();
      ctx.strokeStyle = "#534AB7";
      ctx.beginPath(); ctx.moveTo(x + 5, y + 10); ctx.lineTo(x + 18, y + 10); ctx.stroke();
      ctx.strokeStyle = "#1D9E75";
      ctx.beginPath(); ctx.moveTo(x + 5, y + 14); ctx.lineTo(x + 25, y + 14); ctx.stroke();
    }
    // Helper: filing cabinet
    function cabinet(x: number, y: number) {
      ctx.fillStyle = "#9E9E9E"; ctx.fillRect(x, y, 12, 20);
      ctx.fillStyle = "#BDBDBD"; ctx.fillRect(x + 1, y + 1, 10, 5); ctx.fillRect(x + 1, y + 7, 10, 5); ctx.fillRect(x + 1, y + 13, 10, 5);
      ctx.fillStyle = "#666"; ctx.fillRect(x + 4, y + 3, 4, 1); ctx.fillRect(x + 4, y + 9, 4, 1); ctx.fillRect(x + 4, y + 15, 4, 1);
    }
    // Helper: wall clock (real timezone)
    function wallClock(x: number, y: number) {
      const now = new Date();
      const hours = now.getHours() % 12;
      const minutes = now.getMinutes();
      const seconds = now.getSeconds();
      // Analog face
      ctx.fillStyle = "#FFF"; ctx.beginPath(); ctx.arc(x, y, 14, 0, Math.PI * 2); ctx.fill();
      ctx.strokeStyle = "#333"; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(x, y, 14, 0, Math.PI * 2); ctx.stroke();
      // Hour ticks
      for (let i = 0; i < 12; i++) {
        const a = (i / 12) * Math.PI * 2 - Math.PI / 2;
        ctx.fillStyle = "#555";
        ctx.fillRect(x + Math.cos(a) * 11 - 0.5, y + Math.sin(a) * 11 - 0.5, 1.5, 1.5);
      }
      // Hour hand
      const hAngle = ((hours + minutes / 60) / 12) * Math.PI * 2 - Math.PI / 2;
      ctx.strokeStyle = "#333"; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + Math.cos(hAngle) * 7, y + Math.sin(hAngle) * 7); ctx.stroke();
      // Minute hand
      const mAngle = ((minutes + seconds / 60) / 60) * Math.PI * 2 - Math.PI / 2;
      ctx.strokeStyle = "#333"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + Math.cos(mAngle) * 10, y + Math.sin(mAngle) * 10); ctx.stroke();
      // Second hand
      const sAngle = (seconds / 60) * Math.PI * 2 - Math.PI / 2;
      ctx.strokeStyle = "#D85A30"; ctx.lineWidth = 0.5;
      ctx.beginPath(); ctx.moveTo(x, y); ctx.lineTo(x + Math.cos(sAngle) * 10, y + Math.sin(sAngle) * 10); ctx.stroke();
      // Center dot
      ctx.fillStyle = "#333"; ctx.beginPath(); ctx.arc(x, y, 1.5, 0, Math.PI * 2); ctx.fill();
      // Digital time below
      const timeStr = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      ctx.font = "bold 7px 'Courier New', monospace";
      ctx.fillStyle = "#333"; ctx.textAlign = "center";
      ctx.fillText(timeStr, x, y + 22);
      ctx.textAlign = "left";
    }
    // Helper: lamp
    function lamp(x: number, y: number) {
      ctx.fillStyle = "#888"; ctx.fillRect(x - 1, y - 4, 2, 12);
      ctx.fillStyle = "#666"; ctx.fillRect(x - 4, y + 8, 8, 2);
      ctx.fillStyle = "#FFE082";
      ctx.beginPath(); ctx.moveTo(x - 5, y - 2); ctx.lineTo(x + 5, y - 2); ctx.lineTo(x + 3, y - 7); ctx.lineTo(x - 3, y - 7); ctx.closePath(); ctx.fill();
      ctx.fillStyle = "rgba(255,224,130,0.12)"; ctx.beginPath(); ctx.arc(x, y, 12, 0, Math.PI * 2); ctx.fill();
    }
    // Helper: coffee machine
    function coffee(x: number, y: number) {
      ctx.fillStyle = "#444"; ctx.fillRect(x, y, 16, 18);
      ctx.fillStyle = "#4FC3F7"; ctx.fillRect(x + 3, y + 2, 10, 5);
      ctx.fillStyle = "#F5F5F5"; ctx.fillRect(x + 4, y + 10, 8, 5);
      ctx.fillStyle = "#8B6F47"; ctx.fillRect(x + 5, y + 11, 6, 3);
    }
    // Helper: water cooler
    function cooler(x: number, y: number) {
      ctx.fillStyle = "#E0E0E0"; ctx.fillRect(x - 4, y + 4, 8, 12);
      ctx.fillStyle = "#B3E5FC"; ctx.fillRect(x - 5, y - 2, 10, 8);
      ctx.fillStyle = "#81D4FA"; ctx.fillRect(x - 2, y - 7, 4, 6);
      ctx.fillStyle = "#F44336"; ctx.fillRect(x + 4, y + 1, 2, 2);
    }
    // Helper: conference table
    function confTable(x: number, y: number) {
      ctx.fillStyle = "#A0845C";
      ctx.beginPath(); ctx.roundRect(x, y, 64, 32, 6); ctx.fill();
      ctx.fillStyle = "#8B6F47";
      ctx.beginPath(); ctx.roundRect(x + 2, y + 2, 60, 28, 5); ctx.fill();
      // Chairs around table
      const chairCol = "#7E57C2";
      ctx.fillStyle = chairCol;
      // Top chairs
      ctx.fillRect(x + 12, y - 8, 8, 6); ctx.fillRect(x + 44, y - 8, 8, 6);
      // Bottom chairs
      ctx.fillRect(x + 12, y + 34, 8, 6); ctx.fillRect(x + 44, y + 34, 8, 6);
      // Side chairs
      ctx.fillRect(x - 8, y + 10, 6, 8); ctx.fillRect(x + 66, y + 10, 6, 8);
    }
    // Helper: rug
    function rug(x: number, y: number, w: number, h: number, color: string) {
      ctx.fillStyle = color; ctx.globalAlpha = 0.12;
      ctx.beginPath(); ctx.roundRect(x, y, w, h, 4); ctx.fill();
      ctx.strokeStyle = color; ctx.globalAlpha = 0.2; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect(x + 2, y + 2, w - 4, h - 4, 3); ctx.stroke();
      ctx.globalAlpha = 1;
    }

    // Helper: printer
    function printer(x: number, y: number) {
      ctx.fillStyle = "#E0E0E0"; ctx.fillRect(x, y, 18, 14);
      ctx.fillStyle = "#BDBDBD"; ctx.fillRect(x + 1, y + 1, 16, 4);
      ctx.fillStyle = "#4CAF50"; ctx.fillRect(x + 14, y + 2, 2, 2);
      ctx.fillStyle = "#F5F5F5"; ctx.fillRect(x + 3, y + 7, 12, 3);
      ctx.fillStyle = "#666"; ctx.fillRect(x + 2, y + 12, 14, 2);
    }
    // Helper: sofa / couch
    function sofa(x: number, y: number, color: string) {
      ctx.fillStyle = color; ctx.beginPath(); ctx.roundRect(x, y, 30, 14, 3); ctx.fill();
      ctx.fillStyle = color; ctx.fillRect(x, y - 4, 4, 18); ctx.fillRect(x + 26, y - 4, 4, 18);
      // Cushions
      const lighter = color + "88";
      ctx.fillStyle = lighter; ctx.fillRect(x + 5, y + 2, 9, 8); ctx.fillRect(x + 16, y + 2, 9, 8);
    }
    // Helper: trash bin
    function bin(x: number, y: number) {
      ctx.fillStyle = "#666"; ctx.fillRect(x - 4, y, 8, 10);
      ctx.fillStyle = "#888"; ctx.fillRect(x - 5, y, 10, 2);
    }
    // Helper: picture frame on wall
    function picture(x: number, y: number, color: string) {
      ctx.fillStyle = "#8B6F47"; ctx.fillRect(x, y, 16, 12);
      ctx.fillStyle = color; ctx.fillRect(x + 2, y + 2, 12, 8);
      // Simple landscape
      ctx.fillStyle = "#81C784"; ctx.fillRect(x + 2, y + 6, 12, 4);
      ctx.fillStyle = "#64B5F6"; ctx.fillRect(x + 2, y + 2, 12, 4);
    }
    // Helper: sticky notes on wall
    function stickyNotes(x: number, y: number) {
      ctx.fillStyle = "#FFF176"; ctx.fillRect(x, y, 8, 8);
      ctx.fillStyle = "#A5D6A7"; ctx.fillRect(x + 10, y, 8, 8);
      ctx.fillStyle = "#EF9A9A"; ctx.fillRect(x + 5, y + 10, 8, 8);
      ctx.fillStyle = "#90CAF9"; ctx.fillRect(x + 15, y + 10, 8, 8);
    }
    // Helper: potted cactus
    function cactus(x: number, y: number) {
      ctx.fillStyle = "#A1887F"; ctx.fillRect(x - 3, y + 2, 6, 4);
      ctx.fillStyle = "#66BB6A"; ctx.fillRect(x - 1, y - 6, 3, 9);
      ctx.fillRect(x - 4, y - 3, 3, 2);
      ctx.fillRect(x + 2, y - 5, 3, 2);
    }
    // Helper: monitor/screen on wall (TV)
    function wallScreen(x: number, y: number) {
      ctx.fillStyle = "#333"; ctx.fillRect(x, y, 28, 18);
      ctx.fillStyle = "#1a1a2e";
      const flicker = 0.7 + Math.sin(time * 2) * 0.15;
      ctx.globalAlpha = flicker;
      ctx.fillRect(x + 2, y + 2, 24, 14);
      // Fake chart bars
      ctx.fillStyle = "#534AB7"; ctx.fillRect(x + 4, y + 10, 3, 5);
      ctx.fillStyle = "#1D9E75"; ctx.fillRect(x + 9, y + 7, 3, 8);
      ctx.fillStyle = "#D85A30"; ctx.fillRect(x + 14, y + 5, 3, 10);
      ctx.fillStyle = "#3B82F6"; ctx.fillRect(x + 19, y + 8, 3, 7);
      ctx.globalAlpha = 1;
      ctx.fillStyle = "#555"; ctx.fillRect(x + 12, y + 18, 4, 3);
    }
    // Helper: bean bag
    function beanBag(x: number, y: number, color: string) {
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.ellipse(x, y, 10, 7, 0, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = color + "CC";
      ctx.beginPath(); ctx.ellipse(x, y - 3, 7, 5, 0, Math.PI, Math.PI * 2); ctx.fill();
    }

    // ── CEO Office (0,0 8x8) ──
    rug(1 * T, 2 * T, 6 * T, 4 * T, "#534AB7");
    bookshelf(1 * T + 4, 1 * T + 4);
    lamp(6 * T + 16, 1 * T + 16);
    plant(6 * T + 16, 6 * T + 16, 1.1);
    // clock removed — single wallClock drawn globally
    picture(1 * T + 8, 0 * T + 6, "#E8EAF6");
    cabinet(0 * T + 4, 6 * T + 4);
    cactus(2 * T, 1 * T + 14);
    bin(7 * T, 7 * T);
    sofa(1 * T + 8, 6 * T + 8, "#7E57C2");

    // ── Meeting Room (8,0 8x8) — central conference room ──
    rug(9 * T, 1 * T + 8, 6 * T, 5 * T, "#8B7355");
    confTable(9 * T + 16, 2 * T + 16);
    whiteboard(9 * T + 4, 0 * T + 20);
    plant(15 * T, 1 * T + 16);
    plant(9 * T + 16, 6 * T + 16);
    wallScreen(13 * T + 4, 0 * T + 20);
    stickyNotes(15 * T + 8, 1 * T + 4);
    bin(15 * T + 16, 7 * T);
    cactus(9 * T + 4, 7 * T);

    // ── Content Studio (16,0 8x8) ──
    rug(17 * T, 2 * T, 6 * T, 4 * T, "#1D9E75");
    bookshelf(22 * T, 1 * T + 4);
    whiteboard(17 * T + 4, 1 * T + 4);
    plant(17 * T + 16, 6 * T + 16);
    lamp(22 * T + 16, 6 * T);
    stickyNotes(19 * T, 0 * T + 22);
    printer(17 * T + 4, 6 * T + 4);
    beanBag(23 * T, 3 * T + 8, "#66BB6A");
    cactus(17 * T + 4, 0 * T + 24);
    picture(23 * T, 0 * T + 22, "#C8E6C9");

    // ── Email Center (0,8 8x8) ──
    rug(1 * T, 10 * T, 6 * T, 4 * T, "#BA7517");
    cabinet(1 * T + 4, 9 * T + 4);
    cabinet(1 * T + 20, 9 * T + 4);
    coffee(5 * T + 8, 9 * T + 4);
    plant(6 * T + 16, 14 * T + 16);
    printer(0 * T + 4, 14 * T + 4);
    lamp(1 * T + 8, 14 * T + 16);
    bin(7 * T, 15 * T);
    picture(0 * T + 4, 8 * T + 22, "#FFF3E0");
    cactus(7 * T, 9 * T + 8);
    sofa(3 * T + 8, 14 * T + 10, "#F5A623");

    // ── Social Hub (8,8 8x8) ──
    rug(9 * T, 10 * T, 6 * T, 4 * T, "#D85A30");
    whiteboard(9 * T + 4, 9 * T + 4);
    plant(14 * T + 16, 14 * T + 16);
    lamp(9 * T + 16, 14 * T + 16);
    wallScreen(13 * T + 4, 9 * T + 4);
    beanBag(15 * T, 11 * T, "#E67E22");
    beanBag(10 * T, 14 * T + 8, "#EC407A");
    coffee(14 * T + 8, 9 * T + 4);
    stickyNotes(11 * T, 9 * T + 4);
    bin(9 * T + 4, 15 * T);
    cactus(15 * T + 16, 9 * T + 8);

    // ── Ads Room (16,8 8x8) ──
    rug(17 * T, 10 * T, 6 * T, 4 * T, "#7C3AED");
    bookshelf(17 * T + 4, 9 * T + 4);
    cabinet(22 * T + 8, 9 * T + 4);
    cooler(22 * T + 16, 14 * T + 8);
    plant(17 * T + 16, 14 * T + 16);
    printer(22 * T + 4, 14 * T + 4);
    lamp(17 * T + 8, 9 * T + 16);
    wallScreen(19 * T, 9 * T + 4);
    picture(23 * T, 8 * T + 22, "#EDE7F6");
    bin(23 * T, 15 * T);
    cactus(17 * T + 4, 15 * T);
    sofa(19 * T + 8, 14 * T + 10, "#9575CD");

    // ── Global wall clock — top-left corner, real timezone ──
    wallClock(22, 22);
  }, []);

  // ── Main draw loop ──────────────────────────────────────────────────────

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const time = performance.now() / 1000;
    const pos = posRef.current;

    // Update agent positions + idle wandering
    for (const agent of agentList) {
      const p = pos[agent.id];
      if (!p) continue;

      // Movement toward target — idle agents stroll, others walk
      const speed = agent.status === "idle" ? STROLL_SPEED : WALK_SPEED;
      const dx = p.targetX - p.x;
      const dy = p.targetY - p.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist > 2) {
        p.x += (dx / dist) * speed;
        p.y += (dy / dist) * speed;
        p.walking = true;
        p.walkFrame++;
        p.facingRight = dx > 0;
      } else {
        p.x = p.targetX;
        p.y = p.targetY;
        if (p.walking) {
          p.walking = false;
          p.walkFrame = 0;
        }
      }

      // Thought icon cycling
      p.thoughtTimer--;
      if (p.thoughtTimer <= 0) {
        p.thoughtIcon = (p.thoughtIcon + 1) % THOUGHT_ICONS.length;
        p.thoughtTimer = Math.floor(180 + Math.random() * 240);
      }

      // Wave timer countdown
      if (p.waveTimer > 0) p.waveTimer--;

      // Idle wandering — only when agent status is "idle" and not currently walking
      if (agent.status === "idle" && !p.walking) {
        // If pausing at an idle spot, count down then head home
        if (p.wanderPause > 0) {
          p.wanderPause--;
          if (p.wanderPause <= 0) {
            // Head back to desk
            const deskPx = { x: agent.deskX * T + T / 2, y: agent.deskY * T + T / 2 };
            p.targetX = deskPx.x;
            p.targetY = deskPx.y;
            p.wanderTarget = false;
          }
          continue;
        }

        // If just arrived at an idle spot, start a pause
        if (p.wanderTarget) {
          p.wanderPause = Math.floor(60 + Math.random() * 60);
          continue;
        }

        // Count down idle timer, then pick a new wander spot
        p.idleTimer--;
        if (p.idleTimer <= 0) {
          const spots = IDLE_SPOTS[agent.department];
          if (spots && spots.length > 0) {
            const spot = spots[Math.floor(Math.random() * spots.length)];
            p.targetX = spot.x;
            p.targetY = spot.y;
            p.wanderTarget = true;
          }
          p.idleTimer = Math.floor(300 + Math.random() * 500);
        }
      }
    }

    // Clear
    ctx.clearRect(0, 0, OFFICE_PIXEL_WIDTH, OFFICE_PIXEL_HEIGHT);
    ctx.fillStyle = "#E8E4DE";
    ctx.fillRect(0, 0, OFFICE_PIXEL_WIDTH, OFFICE_PIXEL_HEIGHT);

    // Rooms
    for (const room of ROOMS) drawRoom(ctx, room);

    // Decorations
    drawDecorations(ctx, time);

    // Desks (at desk positions, not agent positions)
    for (const agent of agentList) {
      drawDesk(ctx, agent.deskX * T + T / 2, agent.deskY * T + T / 2);
    }

    // Agents (at animated positions)
    for (const agent of agentList) {
      const p = pos[agent.id];
      if (!p) continue;
      drawAgent(ctx, agent, time, hoveredAgent === agent.id, p.x, p.y, p.walking, p.walkFrame, p.facingRight);

      // Thought bubble for idle wandering agents (not at desk, not walking)
      if (agent.status === "idle" && !p.walking && p.wanderPause > 0) {
        const bubbleX = p.x + 12;
        const bubbleY = p.y - 28 + Math.sin(time * 2) * 2;
        // Cloud shape
        ctx.fillStyle = "rgba(255,255,255,0.9)";
        ctx.beginPath(); ctx.arc(bubbleX, bubbleY, 8, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.arc(bubbleX - 4, bubbleY + 3, 3, 0, Math.PI * 2); ctx.fill();
        ctx.beginPath(); ctx.arc(bubbleX - 7, bubbleY + 7, 2, 0, Math.PI * 2); ctx.fill();
        // Icon inside
        ctx.fillStyle = "#534AB7";
        ctx.font = "bold 8px monospace";
        ctx.textAlign = "center";
        ctx.fillText(THOUGHT_ICONS[p.thoughtIcon], bubbleX, bubbleY + 3);
        ctx.textAlign = "left";
      }

      // Wave on hover
      if (hoveredAgent === agent.id && p.waveTimer <= 0) {
        p.waveTimer = 30; // trigger wave
      }
    }

    frameRef.current = requestAnimationFrame(draw);
  }, [agentList, hoveredAgent, drawRoom, drawDesk, drawAgent, drawDecorations]);

  // ── Mouse ───────────────────────────────────────────────────────────────

  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const scale = scaleRef.current;
    const mx = (e.clientX - rect.left) / scale;
    const my = (e.clientY - rect.top) / scale;
    const pos = posRef.current;

    let found: string | null = null;
    for (const agent of agentList) {
      const p = pos[agent.id];
      if (!p) continue;
      const dist = Math.sqrt((mx - p.x) ** 2 + (my - p.y) ** 2);
      if (dist < 20) { found = agent.id; break; }
    }
    setHoveredAgent(found);
    canvas.style.cursor = found ? "pointer" : "default";
  }, [agentList]);

  const handleClick = useCallback(() => {
    if (hoveredAgent) onAgentClick(hoveredAgent);
  }, [hoveredAgent, onAgentClick]);

  // ── Lifecycle ───────────────────────────────────────────────────────────

  useEffect(() => {
    resize();
    const ro = new ResizeObserver(resize);
    if (wrapRef.current) ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, [resize]);

  useEffect(() => {
    frameRef.current = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(frameRef.current);
  }, [draw]);

  return (
    <div ref={wrapRef} className="absolute inset-0 overflow-hidden">
      <canvas
        ref={canvasRef}
        className="absolute"
        onMouseMove={handleMouseMove}
        onClick={handleClick}
        onMouseLeave={() => setHoveredAgent(null)}
      />
    </div>
  );
}
