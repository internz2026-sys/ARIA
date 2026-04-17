"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import KanbanBoard from "@/components/shared/KanbanBoard";
import { useDraggable } from "@/lib/use-draggable";
import { useResizablePanel, type ResizeCorner } from "@/lib/use-resizable-panel";
import { useTaskUpdates } from "@/lib/socket";
import {
  type Task,
  fetchTasks,
  patchTaskStatus,
  deleteTaskApi,
} from "@/lib/task-config";

export default function OfficeKanban() {
  const [open, setOpen] = useState(false);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const { pos, btnRef, handleMouseDown, handleClick } = useDraggable(
    typeof window !== "undefined" ? window.innerWidth - 200 : 1000,
    typeof window !== "undefined" ? window.innerHeight - 80 : 700,
    "task-board",
  );

  // Listen for real-time task updates from backend
  const taskUpdate = useTaskUpdates(tenantId);

  // Apply real-time task updates to local state
  useEffect(() => {
    if (!taskUpdate) return;
    setTasks((prev) => {
      const exists = prev.some((t) => t.id === taskUpdate.id);
      if (exists) {
        return prev.map((t) => t.id === taskUpdate.id ? { ...t, status: taskUpdate.status } : t);
      }
      // New task — add it
      return [...prev, { id: taskUpdate.id, agent: taskUpdate.agent, task: taskUpdate.task, status: taskUpdate.status, priority: "medium", created_at: new Date().toISOString(), updated_at: new Date().toISOString() }];
    });
  }, [taskUpdate, tenantId]);

  // Load tasks when dropdown opens
  useEffect(() => {
    if (!open) return;
    const tid = localStorage.getItem("aria_tenant_id");
    if (!tid) return;
    setLoading(true);
    fetchTasks(tid).then(setTasks).catch(() => {}).finally(() => setLoading(false));
  }, [open]);

  // Close on click outside (but not when clicking other floating widgets)
  useEffect(() => {
    if (!open) return;
    function h(e: MouseEvent) {
      const t = e.target as HTMLElement;
      if (btnRef.current?.contains(t) || panelRef.current?.contains(t)) return;
      if (t.closest?.("[data-floating-widget]")) return;
      setOpen(false);
    }
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open, btnRef]);

  const onStatusChange = useCallback((id: string, status: string) => {
    setTasks((p) => p.map((t) => (t.id === id ? { ...t, status } : t)));
    patchTaskStatus(id, status);
  }, []);

  const onDelete = useCallback((id: string) => {
    setTasks((p) => p.filter((t) => t.id !== id));
    deleteTaskApi(id);
  }, []);

  const active = tasks.filter((t) => t.status !== "done").length;
  const inProgress = tasks.filter((t) => t.status === "in_progress").length;

  // Panel position + size.
  // - Position follows the button 1:1 (no independent drag, matches
  //   FloatingChat). Drag the button and the panel moves with it.
  // - Size is resizable via a visible corner grip (useResizablePanel hook)
  //   and persisted to localStorage. Restored on next mount.
  const wH = typeof window !== "undefined" ? window.innerHeight : 800;
  const wW = typeof window !== "undefined" ? window.innerWidth : 1200;
  const PANEL_GAP = 8;
  const BUTTON_H = 52;

  const isButtonRight = pos.x > wW * 0.4;
  const isButtonBottom = pos.y > wH * 0.4;
  const corner: ResizeCorner =
    isButtonRight
      ? (isButtonBottom ? "nw" : "sw")
      : (isButtonBottom ? "ne" : "se");

  // Shared between useResizablePanel's direct-DOM path (during drag) and
  // React's render-time style (at rest). Same math both places so the
  // panel doesn't jump when state syncs on mouseup.
  const computePanelPosition = useCallback(
    (s: { w: number; h: number }) => {
      const buttonRightEdge = pos.x + 180; // button is ~180px wide
      const rawPanelX = isButtonRight ? buttonRightEdge - s.w : pos.x;
      const left = Math.min(Math.max(20, rawPanelX), wW - s.w - 20);
      const top = isButtonBottom
        ? Math.max(20, pos.y - s.h - PANEL_GAP)
        : Math.min(wH - s.h - 20, pos.y + BUTTON_H + PANEL_GAP);
      return { left, top };
    },
    [pos.x, pos.y, isButtonRight, isButtonBottom, wW, wH],
  );

  const { size: panelSize, startResize, cursorClass, handles } = useResizablePanel(
    "aria-task-board-panel-size",
    { w: 700, h: 440 },
    corner,
    { minW: 420, minH: 320 },
    { panelRef, computePosition: computePanelPosition },
  );

  const { left: basePanelX, top: basePanelY } = computePanelPosition(panelSize);

  const panelStyle: React.CSSProperties = {
    position: "fixed",
    width: panelSize.w,
    height: panelSize.h,
    left: basePanelX,
    top: basePanelY,
    zIndex: 61,
  };

  const cornerPos = {
    nw: "top-0 left-0",
    ne: "top-0 right-0",
    sw: "bottom-0 left-0",
    se: "bottom-0 right-0",
  }[corner];
  const cornerRound = {
    nw: "rounded-tl-xl",
    ne: "rounded-tr-xl",
    sw: "rounded-bl-xl",
    se: "rounded-br-xl",
  }[corner];

  if (pos.x < 0) return null;

  return (
    <>
      <button
        ref={btnRef}
        data-floating-widget="task-board"
        onMouseDown={handleMouseDown}
        onClick={() => handleClick() && setOpen((v) => !v)}
        className="fixed left-0 top-0 z-[60] flex items-center gap-2.5 h-[52px] px-5 rounded-2xl text-sm font-extrabold tracking-wide select-none cursor-grab active:cursor-grabbing will-change-transform"
        style={{
          transform: `translate3d(${pos.x}px, ${pos.y}px, 0)`,
          background: "linear-gradient(135deg, #FF6B35 0%, #F7418F 100%)",
          color: "#fff",
          boxShadow: "0 8px 30px rgba(255,107,53,0.35), 0 2px 8px rgba(247,65,143,0.2)",
        }}
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2.2} viewBox="0 0 24 24">
          <rect x="3" y="3" width="7" height="7" rx="1.5" />
          <rect x="14" y="3" width="7" height="7" rx="1.5" />
          <rect x="3" y="14" width="7" height="7" rx="1.5" />
          <rect x="14" y="14" width="7" height="7" rx="1.5" />
        </svg>
        Task Board
        {active > 0 && (
          <span className="bg-white/25 text-white text-[11px] font-black px-2.5 py-0.5 rounded-full min-w-[24px] text-center">
            {active}
          </span>
        )}
        {inProgress > 0 && (
          <span className="absolute -top-1 -right-1 w-3.5 h-3.5 rounded-full bg-[#FFD700] border-2 border-white animate-pulse" />
        )}
      </button>

      {open && (
        <div ref={panelRef} data-floating-widget="task-board" style={panelStyle} className="relative bg-white rounded-xl border border-[#E0DED8] shadow-2xl flex flex-col overflow-hidden">
          {/* Resize handles — far edges + far corner, relative to the
              button anchor. Near edges omitted since the panel's near
              side is anchored to the toggle button. */}
          {handles.map((h) => {
            if (h === "n") return <div key={h} onMouseDown={startResize("n")} className="absolute left-0 right-0 top-0 h-1.5 cursor-ns-resize hover:bg-[#FF6B35]/10 z-[62]" />;
            if (h === "s") return <div key={h} onMouseDown={startResize("s")} className="absolute left-0 right-0 bottom-0 h-1.5 cursor-ns-resize hover:bg-[#FF6B35]/10 z-[62]" />;
            if (h === "e") return <div key={h} onMouseDown={startResize("e")} className="absolute top-0 bottom-0 right-0 w-1.5 cursor-ew-resize hover:bg-[#FF6B35]/10 z-[62]" />;
            if (h === "w") return <div key={h} onMouseDown={startResize("w")} className="absolute top-0 bottom-0 left-0 w-1.5 cursor-ew-resize hover:bg-[#FF6B35]/10 z-[62]" />;
            return (
              <div
                key={h}
                onMouseDown={startResize(h)}
                className={`absolute ${cornerPos} w-6 h-6 ${cursorClass} flex items-center justify-center hover:bg-[#FF6B35]/10 ${cornerRound} transition-colors z-[63]`}
                title="Drag to resize"
              >
                <svg
                  className="w-3.5 h-3.5 text-[#FF6B35]/60 pointer-events-none"
                  viewBox="0 0 16 16"
                  fill="none"
                  style={{
                    transform:
                      corner === "ne" ? "scaleX(-1)" :
                      corner === "sw" ? "scaleY(-1)" :
                      corner === "se" ? "rotate(180deg)" :
                      undefined,
                  }}
                >
                  <path d="M1 14 L14 1 M5 14 L14 5 M9 14 L14 9" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              </div>
            );
          })}
          <div
            onMouseDown={(e) => {
              if ((e.target as HTMLElement).closest("button")) return;
              handleMouseDown(e);
            }}
            className="flex items-center justify-between px-4 py-3 border-b border-[#E0DED8] shrink-0 cursor-grab active:cursor-grabbing select-none"
          >
            <div className="flex items-center gap-2">
              <div className="w-2.5 h-2.5 rounded-full" style={{ background: "linear-gradient(135deg, #FF6B35, #F7418F)" }} />
              <h3 className="text-sm font-semibold text-[#2C2C2A]">Task Board</h3>
              {inProgress > 0 && (
                <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-[#FDF3E7] text-[#BA7517]">
                  {inProgress} in progress
                </span>
              )}
            </div>
            <button onClick={() => setOpen(false)} className="text-[#B0AFA8] hover:text-[#2C2C2A] transition-colors">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
          <div className="p-3 flex-1 min-h-0 overflow-y-auto">
            {loading ? (
              <div className="flex items-center justify-center py-8">
                <div className="w-6 h-6 border-2 border-[#FF6B35] border-t-transparent rounded-full animate-spin" />
              </div>
            ) : tasks.length === 0 ? (
              <div className="text-center py-8">
                <p className="text-xs text-[#5F5E5A]">No tasks yet — ask the CEO to delegate work.</p>
              </div>
            ) : (
              <KanbanBoard tasks={tasks} onStatusChange={onStatusChange} onDelete={onDelete} compact />
            )}
          </div>
        </div>
      )}
    </>
  );
}
