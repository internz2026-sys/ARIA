"use client";

import React, { useState, useEffect, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import KanbanBoard from "@/components/shared/KanbanBoard";
import PriorityActionsSection from "@/components/shared/PriorityActionsSection";
import { useConfirm } from "@/lib/use-confirm";
import {
  type Task,
  STATUS_COLUMNS,
  AGENT_LABELS,
  PRIORITY_STYLES,
  fetchTasks,
  fetchTrashedTasks,
  patchTaskStatus,
  deleteTaskApi,
  restoreTaskApi,
  permanentDeleteTaskApi,
} from "@/lib/task-config";

type ViewMode = "table" | "board";
type TabMode = "active" | "trash";
const PAGE_SIZE = 20;

export default function ProjectsPage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewMode, setViewMode] = useState<ViewMode>("table");
  const [page, setPage] = useState(1);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  const [bulkLoading, setBulkLoading] = useState(false);
  const [highlightedId, setHighlightedId] = useState<string | null>(null);
  // Edit Mode gates the bulk-select checkbox column. Off by default so a
  // misclick never reaches a delete; the user has to opt in to bulk
  // operations explicitly. Single-row deletes still go through the
  // confirm dialog below regardless of edit mode.
  const [editMode, setEditMode] = useState(false);
  // Active vs Trash tab — Trash shows soft-deleted tasks with Restore
  // + Delete Forever actions. Switching tabs auto-clears edit mode +
  // selections so a stale checkbox can't act on the wrong list.
  const [tabMode, setTabMode] = useState<TabMode>("active");
  const [trashedTasks, setTrashedTasks] = useState<Task[]>([]);
  const [trashLoading, setTrashLoading] = useState(false);
  const { confirm } = useConfirm();

  // Deep-link support — a notification click for a project / task
  // resource lands here with ?id=<uuid>. Scroll it into view,
  // highlight briefly, and try to expand the target so the user
  // immediately sees what they were alerted about.
  const searchParams = useSearchParams();
  const deepLinkId = searchParams?.get("id") || "";

  useEffect(() => {
    if (!deepLinkId) return;
    if (tasks.length === 0) return;
    if (!tasks.some((t) => t.id === deepLinkId)) return;
    setHighlightedId(deepLinkId);
    requestAnimationFrame(() => {
      const el = document.querySelector(`[data-project-row="${deepLinkId}"]`);
      if (el && typeof (el as any).scrollIntoView === "function") {
        (el as HTMLElement).scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
    const t = setTimeout(() => setHighlightedId(null), 1800);
    return () => clearTimeout(t);
  }, [deepLinkId, tasks]);

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

  const handleDelete = useCallback(async (taskId: string) => {
    // Find the task so the modal can quote a snippet — surfacing what
    // they're about to lose makes accidental confirmations far less
    // likely than a generic "are you sure" prompt.
    const target = tasks.find(t => t.id === taskId);
    const snippet = target?.task ? target.task.slice(0, 120) + (target.task.length > 120 ? "..." : "") : "this task";
    const ok = await confirm({
      title: "Delete this project task?",
      message: `${snippet}\n\nThis permanently removes the task and its agent history. This cannot be undone.`,
      confirmLabel: "Delete task",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    setTasks(prev => prev.filter(t => t.id !== taskId));
    setCheckedIds(prev => { const n = new Set(prev); n.delete(taskId); return n; });
    deleteTaskApi(taskId);
  }, [tasks, confirm]);

  // Pagination
  const totalPages = Math.max(1, Math.ceil(tasks.length / PAGE_SIZE));
  const paginatedTasks = tasks.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);

  // Bulk actions
  const toggleCheck = (id: string) => {
    setCheckedIds(prev => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id); else n.add(id);
      return n;
    });
  };

  const toggleAll = () => {
    if (checkedIds.size === paginatedTasks.length) {
      setCheckedIds(new Set());
    } else {
      setCheckedIds(new Set(paginatedTasks.map(t => t.id)));
    }
  };

  // Lazy-fetch trashed tasks when the user switches to the Trash tab.
  useEffect(() => {
    if (tabMode !== "trash") return;
    const tid = localStorage.getItem("aria_tenant_id");
    if (!tid) return;
    setTrashLoading(true);
    fetchTrashedTasks(tid)
      .then(setTrashedTasks)
      .catch(() => setTrashedTasks([]))
      .finally(() => setTrashLoading(false));
  }, [tabMode]);

  const handleRestore = useCallback(async (taskId: string) => {
    setTrashedTasks((prev) => prev.filter((t) => t.id !== taskId));
    try {
      await restoreTaskApi(taskId);
      // Refresh active list so the restored task appears.
      const tid = localStorage.getItem("aria_tenant_id");
      if (tid) fetchTasks(tid).then(setTasks).catch(() => {});
    } catch {
      // On failure, refetch trash so the row reappears in case the
      // optimistic remove was wrong.
      const tid = localStorage.getItem("aria_tenant_id");
      if (tid) fetchTrashedTasks(tid).then(setTrashedTasks).catch(() => {});
    }
  }, []);

  const handlePermanentDelete = useCallback(async (task: Task) => {
    const ok = await confirm({
      title: "Delete this task forever?",
      message: `${task.task.slice(0, 120)}${task.task.length > 120 ? "..." : ""}\n\nThis is permanent — the task and its agent history will be gone for good. This cannot be undone.`,
      confirmLabel: "Delete forever",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    setTrashedTasks((prev) => prev.filter((t) => t.id !== task.id));
    try {
      await permanentDeleteTaskApi(task.id);
    } catch {
      const tid = localStorage.getItem("aria_tenant_id");
      if (tid) fetchTrashedTasks(tid).then(setTrashedTasks).catch(() => {});
    }
  }, [confirm]);

  const handleBulkDelete = async () => {
    if (checkedIds.size === 0) return;
    const n = checkedIds.size;
    const ok = await confirm({
      title: `Delete ${n} ${n === 1 ? "task" : "tasks"}?`,
      message: `This permanently removes ${n === 1 ? "this task" : `all ${n} selected tasks`} and ${n === 1 ? "its" : "their"} agent history. This cannot be undone.`,
      confirmLabel: `Delete ${n} ${n === 1 ? "task" : "tasks"}`,
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    setBulkLoading(true);
    await Promise.all(Array.from(checkedIds).map(id => deleteTaskApi(id)));
    setTasks(prev => prev.filter(t => !checkedIds.has(t.id)));
    setCheckedIds(new Set());
    setBulkLoading(false);
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div>
      {/* Stagnation Monitor — pinned at the very top so buried drafts
          aren't lost behind the Kanban / table when newer work piles
          on. Self-hides when there are zero stale items. */}
      <PriorityActionsSection />

      {/* Header */}
      <div className="flex items-center justify-between mb-6 gap-3 flex-wrap">
        <div>
          <h1 className="text-xl sm:text-2xl font-bold text-[#2C2C2A]">Projects</h1>
          <p className="text-sm text-[#5F5E5A] mt-1">
            {tabMode === "trash"
              ? "Soft-deleted tasks. Restore to bring them back, or delete forever."
              : "Tasks delegated by the CEO agent to your marketing team"}
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          {/* Active / Trash tab toggle. Switching to Trash auto-exits
              edit mode + drops selections so a stale checked id from
              the Active tab can't act on a Trash row. */}
          <div className="flex items-center gap-1 bg-[#F8F8F6] rounded-lg p-1 border border-[#E0DED8]">
            <button
              onClick={() => { setTabMode("active"); }}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition ${tabMode === "active" ? "bg-white text-[#2C2C2A] shadow-sm" : "text-[#5F5E5A] hover:text-[#2C2C2A]"}`}
            >
              Active
            </button>
            <button
              onClick={() => { setTabMode("trash"); setEditMode(false); setCheckedIds(new Set()); }}
              className={`px-3 py-1.5 rounded-md text-xs font-medium transition ${tabMode === "trash" ? "bg-white text-[#2C2C2A] shadow-sm" : "text-[#5F5E5A] hover:text-[#2C2C2A]"}`}
            >
              Trash
            </button>
          </div>
          {/* Manage / Done toggle — gates the bulk-select checkbox column
              in the table. Hidden in Board view since there's no bulk
              selection there. Hidden in Trash view since trash rows
              have their own Restore + Delete Forever actions per row.
              Auto-clears any in-flight selection when leaving edit mode
              so a stray checked item can't be deleted from a "Done"
              state. */}
          {tabMode === "active" && viewMode === "table" && (
            <button
              onClick={() => {
                setEditMode((m) => {
                  if (m) setCheckedIds(new Set());
                  return !m;
                });
              }}
              className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border transition ${
                editMode
                  ? "border-[#534AB7] bg-[#EEEDFE] text-[#534AB7]"
                  : "border-[#E0DED8] bg-white text-[#5F5E5A] hover:border-[#534AB7]/40 hover:text-[#534AB7]"
              }`}
              title={editMode ? "Exit bulk-select mode" : "Enable checkboxes for bulk actions"}
            >
              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
              {editMode ? "Done" : "Manage"}
            </button>
          )}
          {/* Table / Board switcher hidden in Trash — trashed rows
              don't have a Kanban representation. */}
          {tabMode === "active" && (
            <div className="flex items-center gap-1 bg-[#F8F8F6] rounded-lg p-1 border border-[#E0DED8]">
              <button
                onClick={() => setViewMode("table")}
                className={`px-3 py-1.5 rounded-md text-xs font-medium transition ${viewMode === "table" ? "bg-white text-[#2C2C2A] shadow-sm" : "text-[#5F5E5A] hover:text-[#2C2C2A]"}`}
              >
                Table
              </button>
              <button
                onClick={() => { setViewMode("board"); setEditMode(false); setCheckedIds(new Set()); }}
                className={`px-3 py-1.5 rounded-md text-xs font-medium transition ${viewMode === "board" ? "bg-white text-[#2C2C2A] shadow-sm" : "text-[#5F5E5A] hover:text-[#2C2C2A]"}`}
              >
                Board
              </button>
            </div>
          )}
        </div>
      </div>

      {tabMode === "trash" ? (
        <TrashView
          tasks={trashedTasks}
          loading={trashLoading}
          onRestore={handleRestore}
          onPermanentDelete={handlePermanentDelete}
        />
      ) : tasks.length === 0 ? (
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
        <>
          {/* Bulk actions bar — only visible in Edit Mode AND with at
              least one selection. Belt-and-braces: even though
              checkedIds is auto-cleared when leaving edit mode, the
              `editMode &&` guard prevents an in-flight optimistic
              update from briefly flashing the bar. */}
          {editMode && checkedIds.size > 0 && (
            <div className="mb-3 flex items-center gap-3 px-4 py-2.5 bg-[#EEEDFE] rounded-lg border border-[#534AB7]/20">
              <span className="text-xs font-semibold text-[#534AB7]">{checkedIds.size} selected</span>
              <button
                onClick={handleBulkDelete}
                disabled={bulkLoading}
                className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors disabled:opacity-60"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                </svg>
                {bulkLoading ? "Deleting..." : "Delete"}
              </button>
              <button onClick={() => setCheckedIds(new Set())} className="text-xs text-[#5F5E5A] hover:text-[#2C2C2A] ml-auto">
                Clear
              </button>
            </div>
          )}

          <TableView
            tasks={paginatedTasks}
            onStatusChange={updateStatus}
            onDelete={handleDelete}
            checkedIds={checkedIds}
            onToggleCheck={toggleCheck}
            onToggleAll={toggleAll}
            allChecked={checkedIds.size === paginatedTasks.length && paginatedTasks.length > 0}
            highlightedId={highlightedId}
            editMode={editMode}
          />

          {/* Pagination */}
          {totalPages > 1 && (
            <div className="flex items-center justify-between mt-4 px-1">
              <p className="text-xs text-[#9E9C95]">
                Showing {(page - 1) * PAGE_SIZE + 1}–{Math.min(page * PAGE_SIZE, tasks.length)} of {tasks.length}
              </p>
              <div className="flex items-center gap-1">
                <button
                  onClick={() => setPage(p => Math.max(1, p - 1))}
                  disabled={page === 1}
                  className="px-3 py-1.5 text-xs font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] disabled:opacity-40 transition-colors"
                >
                  Previous
                </button>
                {Array.from({ length: totalPages }, (_, i) => i + 1).map(p => (
                  <button
                    key={p}
                    onClick={() => setPage(p)}
                    className={`w-8 h-8 rounded-lg text-xs font-medium transition-colors ${
                      p === page ? "bg-[#534AB7] text-white" : "text-[#5F5E5A] hover:bg-[#F8F8F6]"
                    }`}
                  >
                    {p}
                  </button>
                ))}
                <button
                  onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                  disabled={page === totalPages}
                  className="px-3 py-1.5 text-xs font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] disabled:opacity-40 transition-colors"
                >
                  Next
                </button>
              </div>
            </div>
          )}
        </>
      ) : (
        <KanbanBoard tasks={tasks} onStatusChange={updateStatus} onDelete={handleDelete} />
      )}
    </div>
  );
}

/* ─── Table View ─── */
function TableView({
  tasks, onStatusChange, onDelete, checkedIds, onToggleCheck, onToggleAll, allChecked,
  highlightedId, editMode,
}: {
  tasks: Task[];
  onStatusChange: (id: string, s: string) => void;
  onDelete: (id: string) => void;
  checkedIds: Set<string>;
  onToggleCheck: (id: string) => void;
  onToggleAll: () => void;
  allChecked: boolean;
  highlightedId: string | null;
  editMode: boolean;
}) {
  return (
    <>
      {/* Mobile: stacked card layout. The wide 720px-min-width table
          below is unreadable on phones — long delegation prompts pile
          up dozens of wrapped lines per row. The card view truncates
          to 2 lines and keeps every action accessible. */}
      <div className="sm:hidden space-y-2">
        {tasks.map(task => {
          const agent = AGENT_LABELS[task.agent] || { name: task.agent, color: "#5F5E5A" };
          const priority = PRIORITY_STYLES[task.priority] || PRIORITY_STYLES.medium;
          const checked = checkedIds.has(task.id);
          return (
            <div
              key={task.id}
              data-project-row={task.id}
              className={`bg-white rounded-lg border p-3 transition ${
                highlightedId === task.id
                  ? "border-[#534AB7]/40 ring-2 ring-inset ring-[#534AB7]/40 animate-pulse"
                  : checked
                    ? "border-[#534AB7]/30 bg-[#EEEDFE]/30"
                    : "border-[#E0DED8]"
              }`}
            >
              <div className="flex items-start gap-2">
                {editMode && (
                  <input
                    type="checkbox"
                    checked={checked}
                    onChange={() => onToggleCheck(task.id)}
                    className="mt-0.5 w-4 h-4 rounded border-[#E0DED8] text-[#534AB7] focus:ring-[#534AB7]/30 cursor-pointer shrink-0"
                    aria-label="Select task"
                  />
                )}
                <div className="flex-1 min-w-0">
                  <p className="text-sm text-[#2C2C2A] leading-snug line-clamp-2">
                    {task.title || task.task}
                  </p>
                  <div className="flex items-center gap-1.5 mt-2 flex-wrap">
                    <span
                      className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded-full"
                      style={{ backgroundColor: agent.color + "15", color: agent.color }}
                    >
                      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: agent.color }} />
                      {agent.name}
                    </span>
                    <span
                      className="text-[10px] font-medium px-1.5 py-0.5 rounded-full"
                      style={{ backgroundColor: priority.bg, color: priority.color }}
                    >
                      {priority.label}
                    </span>
                    <span className="text-[10px] text-[#9E9C95]">
                      {new Date(task.created_at).toLocaleDateString()}
                    </span>
                  </div>
                </div>
                <button
                  onClick={() => onDelete(task.id)}
                  className="shrink-0 p-1 text-[#B0AFA8] hover:text-[#D85A30] transition"
                  title="Delete task"
                  aria-label="Delete task"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                  </svg>
                </button>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <select
                  value={task.status}
                  onChange={e => onStatusChange(task.id, e.target.value)}
                  className="flex-1 text-[11px] font-medium px-2 py-1.5 rounded-md border border-[#E0DED8] bg-white text-[#2C2C2A] outline-none cursor-pointer hover:border-[#534AB7]/40 transition"
                  aria-label="Task status"
                >
                  {STATUS_COLUMNS.map(s => (
                    <option key={s.key} value={s.key}>{s.label}</option>
                  ))}
                </select>
                {task.inbox_item_id && (
                  <a
                    href={`/inbox?id=${task.inbox_item_id}`}
                    className="text-[11px] font-semibold px-2.5 py-1.5 rounded-md border border-[#534AB7]/30 text-[#534AB7] bg-[#EEEDFE] hover:bg-[#534AB7] hover:text-white transition-colors shrink-0"
                    title="Open the copy-paste instructions in the Inbox"
                  >
                    Review →
                  </a>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* sm+: classic wide table */}
      <div className="hidden sm:block bg-white rounded-xl border border-[#E0DED8] overflow-x-auto">
        <table className="w-full min-w-[720px]">
        <thead>
          <tr className="border-b border-[#E0DED8] bg-[#F8F8F6]">
            {/* Checkbox column header — only present in Edit Mode so
                the table layout collapses cleanly when the checkboxes
                are hidden, instead of leaving an empty 40px gutter. */}
            {editMode && (
              <th className="w-[40px] px-4 py-3">
                <input
                  type="checkbox"
                  checked={allChecked}
                  onChange={onToggleAll}
                  className="w-4 h-4 rounded border-[#E0DED8] text-[#534AB7] focus:ring-[#534AB7]/30 cursor-pointer"
                />
              </th>
            )}
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
            const checked = checkedIds.has(task.id);
            return (
              <tr
                key={task.id}
                data-project-row={task.id}
                className={`border-b border-[#E0DED8] last:border-0 transition ${
                  highlightedId === task.id
                    ? "bg-[#EEEDFE] ring-2 ring-inset ring-[#534AB7]/40 animate-pulse"
                    : checked ? "bg-[#EEEDFE]/30" : "hover:bg-[#F8F8F6]/50"
                }`}
              >
                {editMode && (
                  <td className="px-4 py-3">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => onToggleCheck(task.id)}
                      className="w-4 h-4 rounded border-[#E0DED8] text-[#534AB7] focus:ring-[#534AB7]/30 cursor-pointer"
                    />
                  </td>
                )}
                <td className="px-4 py-3">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-[#2C2C2A] leading-relaxed">
                        {task.title || task.task}
                      </p>
                      {task.metadata && typeof task.metadata === "object" ? (
                        <div className="mt-1 flex items-center gap-2 flex-wrap">
                          {(task.metadata as any).campaign_objective ? (
                            <span className="text-[10px] font-medium text-[#5F5E5A] bg-[#F8F8F6] border border-[#E0DED8] rounded-full px-2 py-0.5">
                              {String((task.metadata as any).campaign_objective)}
                            </span>
                          ) : null}
                          {(task.metadata as any).projected_budget ? (
                            <span className="text-[10px] font-medium text-[#1D9E75] bg-[#E6F7F0] rounded-full px-2 py-0.5">
                              {String((task.metadata as any).projected_budget)}
                            </span>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                    {task.inbox_item_id ? (
                      <a
                        href={`/inbox?id=${task.inbox_item_id}`}
                        className="shrink-0 text-[11px] font-semibold px-2.5 py-1 rounded-md border border-[#534AB7]/30 text-[#534AB7] bg-[#EEEDFE] hover:bg-[#534AB7] hover:text-white transition-colors"
                        title="Open the copy-paste instructions in the Inbox"
                      >
                        Review
                      </a>
                    ) : null}
                  </div>
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
    </>
  );
}

/* ─── Trash View ─── */
function TrashView({
  tasks, loading, onRestore, onPermanentDelete,
}: {
  tasks: Task[];
  loading: boolean;
  onRestore: (id: string) => void;
  onPermanentDelete: (task: Task) => void;
}) {
  if (loading) {
    return (
      <div className="bg-white rounded-xl border border-[#E0DED8] p-12 text-center">
        <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin mx-auto" />
      </div>
    );
  }
  if (tasks.length === 0) {
    return (
      <div className="bg-white rounded-xl border border-[#E0DED8] p-12 text-center">
        <div className="w-12 h-12 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-4">
          <svg className="w-6 h-6 text-[#9E9C95]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
          </svg>
        </div>
        <p className="text-[#2C2C2A] font-semibold mb-1">Trash is empty</p>
        <p className="text-sm text-[#5F5E5A]">Deleted tasks land here. You can restore them or delete forever.</p>
      </div>
    );
  }
  return (
    <div className="bg-white rounded-xl border border-[#E0DED8] divide-y divide-[#E0DED8]">
      {tasks.map((t) => {
        const agent = AGENT_LABELS[t.agent] || { name: t.agent, color: "#5F5E5A" };
        const deletedAt = (t as any).deleted_at as string | undefined;
        const deletedDisplay = deletedAt ? new Date(deletedAt).toLocaleDateString() : "—";
        return (
          <div
            key={t.id}
            className="flex items-start gap-3 px-4 py-3 hover:bg-[#FAFAFA] transition"
          >
            <div className="flex-1 min-w-0">
              <p className="text-sm text-[#2C2C2A] leading-relaxed line-clamp-2">{t.task}</p>
              <div className="flex items-center gap-2 mt-1.5 flex-wrap">
                <span className="inline-flex items-center gap-1.5 text-[10px] font-medium px-1.5 py-0.5 rounded-full" style={{ backgroundColor: agent.color + "15", color: agent.color }}>
                  <span className="w-1 h-1 rounded-full" style={{ backgroundColor: agent.color }} />
                  {agent.name}
                </span>
                <span className="text-[10px] text-[#9E9C95]">deleted {deletedDisplay}</span>
              </div>
            </div>
            <div className="flex items-center gap-1.5 shrink-0">
              <button
                onClick={() => onRestore(t.id)}
                className="text-xs px-2.5 py-1 rounded-md border border-[#1D9E75]/30 text-[#157A5A] bg-white hover:bg-[#E6F5ED] transition-colors"
                title="Move back to active tasks"
              >
                Restore
              </button>
              <button
                onClick={() => onPermanentDelete(t)}
                className="text-xs px-2.5 py-1 rounded-md border border-[#D85A30]/30 text-[#B8491F] bg-white hover:bg-[#FDEEE8] transition-colors"
                title="Delete forever — cannot be undone"
              >
                Delete forever
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}
