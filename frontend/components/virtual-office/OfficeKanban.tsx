"use client";

import React, { useState, useEffect, useRef, useCallback } from "react";
import KanbanBoard from "@/components/shared/KanbanBoard";
import { useDraggable } from "@/lib/use-draggable";
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

  const { pos, btnRef, handleMouseDown, handleClick } = useDraggable(
    typeof window !== "undefined" ? window.innerWidth - 200 : 1000,
    typeof window !== "undefined" ? window.innerHeight - 80 : 700,
  );

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

  // Panel position — offset from button so it follows when button is dragged
  const wH = typeof window !== "undefined" ? window.innerHeight : 800;
  const wW = typeof window !== "undefined" ? window.innerWidth : 1200;
  const [panelOffset, setPanelOffset] = useState<{ dx: number; dy: number }>({ dx: 0, dy: 0 });
  const panelDragRef = useRef<{ startX: number; startY: number; startDx: number; startDy: number } | null>(null);

  useEffect(() => { if (open) setPanelOffset({ dx: 0, dy: 0 }); }, [open]);

  const basePanelX = Math.max(20, pos.x > wW * 0.4 ? pos.x + 180 - 700 : pos.x);
  const basePanelY = pos.y > wH * 0.4 ? Math.max(20, pos.y - 440 - 12) : pos.y + 56 + 12;

  const panelStyle: React.CSSProperties = {
    position: "fixed",
    width: 700,
    maxWidth: "calc(100vw - 40px)",
    left: basePanelX + panelOffset.dx,
    top: basePanelY + panelOffset.dy,
    zIndex: 61,
  };

  const onPanelHeaderDown = useCallback((e: React.MouseEvent) => {
    if ((e.target as HTMLElement).closest("button")) return;
    e.preventDefault();
    panelDragRef.current = { startX: e.clientX, startY: e.clientY, startDx: panelOffset.dx, startDy: panelOffset.dy };
    function onMove(ev: MouseEvent) {
      const d = panelDragRef.current!;
      setPanelOffset({ dx: d.startDx + ev.clientX - d.startX, dy: d.startDy + ev.clientY - d.startY });
    }
    function onUp() { panelDragRef.current = null; document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [panelOffset]);

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
        <div ref={panelRef} data-floating-widget="task-board" style={panelStyle} className="bg-white rounded-xl border border-[#E0DED8] shadow-2xl">
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#E0DED8] cursor-grab active:cursor-grabbing" onMouseDown={onPanelHeaderDown}>
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
          <div className="p-3 max-h-[380px] overflow-y-auto">
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
