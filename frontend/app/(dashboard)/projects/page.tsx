"use client";

import React, { useState, useEffect, useCallback } from "react";
import KanbanBoard from "@/components/shared/KanbanBoard";
import {
  type Task,
  STATUS_COLUMNS,
  AGENT_LABELS,
  PRIORITY_STYLES,
  fetchTasks,
  patchTaskStatus,
  deleteTaskApi,
} from "@/lib/task-config";

type ViewMode = "table" | "board";

export default function ProjectsPage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewMode, setViewMode] = useState<ViewMode>("table");

  useEffect(() => {
    const tid = localStorage.getItem("aria_tenant_id");
    if (!tid) { setLoading(false); return; }
    fetchTasks(tid)
      .then(setTasks)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  const updateStatus = useCallback((taskId: string, status: string) => {
    setTasks(prev => prev.map(t => t.id === taskId ? { ...t, status } : t));
    patchTaskStatus(taskId, status);
  }, []);

  const handleDelete = useCallback((taskId: string) => {
    setTasks(prev => prev.filter(t => t.id !== taskId));
    deleteTaskApi(taskId);
  }, []);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div>
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-bold text-[#2C2C2A]">Projects</h1>
          <p className="text-sm text-[#5F5E5A] mt-1">Tasks delegated by the CEO agent to your marketing team</p>
        </div>
        <div className="flex items-center gap-1 bg-[#F8F8F6] rounded-lg p-1 border border-[#E0DED8]">
          <button
            onClick={() => setViewMode("table")}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition ${viewMode === "table" ? "bg-white text-[#2C2C2A] shadow-sm" : "text-[#5F5E5A] hover:text-[#2C2C2A]"}`}
          >
            Table
          </button>
          <button
            onClick={() => setViewMode("board")}
            className={`px-3 py-1.5 rounded-md text-xs font-medium transition ${viewMode === "board" ? "bg-white text-[#2C2C2A] shadow-sm" : "text-[#5F5E5A] hover:text-[#2C2C2A]"}`}
          >
            Board
          </button>
        </div>
      </div>

      {tasks.length === 0 ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] p-12 text-center">
          <div className="w-12 h-12 rounded-full bg-[#EEEDFE] flex items-center justify-center mx-auto mb-4">
            <svg className="w-6 h-6 text-[#534AB7]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
            </svg>
          </div>
          <p className="text-[#2C2C2A] font-semibold mb-1">No tasks yet</p>
          <p className="text-sm text-[#5F5E5A]">Ask the CEO agent to create content, campaigns, or ads — tasks will appear here.</p>
        </div>
      ) : viewMode === "table" ? (
        <TableView tasks={tasks} onStatusChange={updateStatus} onDelete={handleDelete} />
      ) : (
        <KanbanBoard tasks={tasks} onStatusChange={updateStatus} onDelete={handleDelete} />
      )}
    </div>
  );
}

/* ─── Table View ─── */
function TableView({ tasks, onStatusChange, onDelete }: { tasks: Task[]; onStatusChange: (id: string, s: string) => void; onDelete: (id: string) => void }) {
  return (
    <div className="bg-white rounded-xl border border-[#E0DED8] overflow-hidden">
      <table className="w-full">
        <thead>
          <tr className="border-b border-[#E0DED8] bg-[#F8F8F6]">
            <th className="text-left px-4 py-3 text-xs font-semibold text-[#5F5E5A] uppercase tracking-wide">Task</th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-[#5F5E5A] uppercase tracking-wide w-[140px]">Agent</th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-[#5F5E5A] uppercase tracking-wide w-[100px]">Priority</th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-[#5F5E5A] uppercase tracking-wide w-[140px]">Status</th>
            <th className="text-left px-4 py-3 text-xs font-semibold text-[#5F5E5A] uppercase tracking-wide w-[110px]">Created</th>
            <th className="w-[50px]"></th>
          </tr>
        </thead>
        <tbody>
          {tasks.map(task => {
            const agent = AGENT_LABELS[task.agent] || { name: task.agent, color: "#5F5E5A" };
            const priority = PRIORITY_STYLES[task.priority] || PRIORITY_STYLES.medium;
            return (
              <tr key={task.id} className="border-b border-[#E0DED8] last:border-0 hover:bg-[#F8F8F6]/50 transition">
                <td className="px-4 py-3">
                  <p className="text-sm text-[#2C2C2A] leading-relaxed">{task.task}</p>
                </td>
                <td className="px-4 py-3">
                  <span className="inline-flex items-center gap-1.5 text-xs font-medium px-2 py-1 rounded-full" style={{ backgroundColor: agent.color + "15", color: agent.color }}>
                    <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: agent.color }} />
                    {agent.name}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <span className="text-xs font-medium px-2 py-1 rounded-full" style={{ backgroundColor: priority.bg, color: priority.color }}>
                    {priority.label}
                  </span>
                </td>
                <td className="px-4 py-3">
                  <select
                    value={task.status}
                    onChange={e => onStatusChange(task.id, e.target.value)}
                    className="text-xs font-medium px-2 py-1.5 rounded-lg border border-[#E0DED8] bg-white text-[#2C2C2A] outline-none cursor-pointer hover:border-[#534AB7]/40 transition"
                  >
                    {STATUS_COLUMNS.map(s => (
                      <option key={s.key} value={s.key}>{s.label}</option>
                    ))}
                  </select>
                </td>
                <td className="px-4 py-3">
                  <span className="text-xs text-[#5F5E5A]">{new Date(task.created_at).toLocaleDateString()}</span>
                </td>
                <td className="px-4 py-3">
                  <button onClick={() => onDelete(task.id)} className="text-[#B0AFA8] hover:text-[#D85A30] transition" title="Delete task">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
