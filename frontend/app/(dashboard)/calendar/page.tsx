"use client";

import React, { useState, useEffect, useCallback } from "react";
import { useSearchParams } from "next/navigation";
import { API_URL, authFetch } from "@/lib/api";
import { useConfirm } from "@/lib/use-confirm";
import { useNotifications } from "@/lib/use-notifications";

// ─── Types ──────────────────────────────────────────────────────────────────

interface ScheduledTask {
  id: string;
  task_type: string;
  title: string;
  scheduled_at: string;
  timezone: string;
  status: string;
  approval_status: string;
  created_by: string;
  related_entity_type?: string;
  payload?: Record<string, any>;
  // Activity-feed fields populated when source isn't 'scheduled'.
  // These let the same component handle multiple event sources without
  // a separate parallel state tree.
  source?: "scheduled" | "inbox_draft" | "inbox_sent";
  agent?: string;
  href?: string;
}

type ViewMode = "month" | "week" | "agenda";
type FilterMode = "all" | "scheduled";

// Strip the source prefix from a composite event id ("scheduled:uuid"
// -> "uuid") so we can call the scheduler endpoints with the raw uuid.
function rawTaskId(id: string): string {
  if (id.includes(":")) return id.split(":").slice(1).join(":");
  return id;
}

// Keys must match the backend's task_type values from
// backend/services/scheduler.py executor switch.
// Both with and without `_task` suffix are listed because earlier code
// used the suffixed form -- support both so old rows still render.
//
// The 'inbox_draft' / 'inbox_sent' / 'agent_run' keys are activity-feed
// pseudo-types used by the /api/calendar/{tenant}/activity endpoint to
// represent inbox items as calendar events.
const TASK_TYPE_COLORS: Record<string, { bg: string; text: string; label: string }> = {
  send_email: { bg: "bg-blue-100", text: "text-blue-700", label: "Email" },
  publish_post: { bg: "bg-purple-100", text: "text-purple-700", label: "Post" },
  publish_campaign: { bg: "bg-orange-100", text: "text-orange-700", label: "Campaign" },
  follow_up: { bg: "bg-emerald-100", text: "text-emerald-700", label: "Follow-up" },
  follow_up_task: { bg: "bg-emerald-100", text: "text-emerald-700", label: "Follow-up" },
  reminder: { bg: "bg-amber-100", text: "text-amber-700", label: "Reminder" },
  reminder_task: { bg: "bg-amber-100", text: "text-amber-700", label: "Reminder" },
  // Activity feed pseudo-types
  inbox_draft: { bg: "bg-gray-100", text: "text-gray-700", label: "Draft" },
  inbox_sent: { bg: "bg-emerald-100", text: "text-emerald-700", label: "Sent" },
  agent_run: { bg: "bg-purple-100", text: "text-purple-700", label: "Agent" },
};

const STATUS_COLORS: Record<string, string> = {
  draft: "bg-gray-200 text-gray-600",
  pending_approval: "bg-yellow-100 text-yellow-700",
  approved: "bg-blue-100 text-blue-700",
  scheduled: "bg-[#EEEDFE] text-[#534AB7]",
  running: "bg-blue-200 text-blue-800",
  sent: "bg-emerald-100 text-emerald-700",
  published: "bg-emerald-100 text-emerald-700",
  failed: "bg-red-100 text-red-700",
  cancelled: "bg-gray-100 text-gray-400",
};

// ─── Helpers ────────────────────────────────────────────────────────────────

function startOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), 1);
}
function endOfMonth(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth() + 1, 0, 23, 59, 59);
}
function startOfWeek(d: Date): Date {
  const day = d.getDay();
  const diff = d.getDate() - day;
  return new Date(d.getFullYear(), d.getMonth(), diff);
}
function addDays(d: Date, n: number): Date {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}
function isSameDay(a: Date, b: Date): boolean {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}
function formatTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", hour12: true });
}
function formatDate(d: Date): string {
  return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

// ─── Component ──────────────────────────────────────────────────────────────

export default function CalendarPage() {
  const { confirm } = useConfirm();
  const { showToast } = useNotifications();
  const [view, setView] = useState<ViewMode>("month");
  // Filter mode: "all" (activity dashboard: scheduled + inbox + sent)
  // vs "scheduled" (only things explicitly queued for execution).
  // Default to "all" so the calendar feels useful even when the user
  // hasn't explicitly scheduled anything yet.
  const [filterMode, setFilterMode] = useState<FilterMode>("all");
  const [currentDate, setCurrentDate] = useState(new Date());
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<ScheduledTask | null>(null);
  // For "+N more" expand-day modal
  const [expandedDay, setExpandedDay] = useState<{ date: Date; tasks: ScheduledTask[] } | null>(null);
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  // Deep-link: notification clicks for a scheduled_task land here
  // with ?id=<uuid>. Select the row so the detail pane opens; if the
  // task isn't in the current view, show a toast.
  const searchParams = useSearchParams();
  const deepLinkId = searchParams?.get("id") || "";

  useEffect(() => {
    if (!deepLinkId) return;
    if (tasks.length === 0) return;
    const found = tasks.find((t) => t.id === deepLinkId);
    if (found) {
      setSelected(found);
      requestAnimationFrame(() => {
        const el = document.querySelector(`[data-calendar-event="${deepLinkId}"]`);
        if (el && typeof (el as any).scrollIntoView === "function") {
          (el as HTMLElement).scrollIntoView({ behavior: "smooth", block: "center" });
        }
      });
    }
  }, [deepLinkId, tasks]);

  const fetchTasks = useCallback(async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      let start: string, end: string;
      if (view === "month") {
        const ms = startOfMonth(currentDate);
        const me = endOfMonth(currentDate);
        // Extend to full weeks
        start = startOfWeek(ms).toISOString();
        end = addDays(me, 7 - me.getDay()).toISOString();
      } else if (view === "week") {
        const ws = startOfWeek(currentDate);
        start = ws.toISOString();
        end = addDays(ws, 7).toISOString();
      } else {
        start = new Date().toISOString();
        end = addDays(new Date(), 30).toISOString();
      }

      if (filterMode === "all") {
        // Unified marketing activity feed: scheduled tasks + inbox
        // drafts + sent items, all in one normalized event shape.
        const res = await authFetch(`${API_URL}/api/calendar/${tenantId}/activity?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
        const data = await res.json();
        // Map activity events into the ScheduledTask shape so the
        // existing render code (TaskCard, MonthView, etc) works
        // unchanged. The `source` field tells the renderer which
        // colors/labels to use.
        const events: ScheduledTask[] = (data.events || []).map((e: any) => ({
          id: e.id,
          task_type: e.source === "scheduled" ? (e.task_type || "reminder") : e.source,
          title: e.title || "(untitled)",
          scheduled_at: e.timestamp,
          timezone: e.metadata?.timezone || "UTC",
          status: e.status || "",
          approval_status: e.approval_status || "",
          created_by: e.metadata?.created_by || "",
          payload: e.metadata || {},
          source: e.source,
          agent: e.agent,
          href: e.href,
        }));
        setTasks(events);
      } else {
        const res = await authFetch(`${API_URL}/api/schedule/${tenantId}/calendar?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
        const data = await res.json();
        const scheduled: ScheduledTask[] = (data.tasks || []).map((t: any) => ({ ...t, source: "scheduled" as const }));
        setTasks(scheduled);
      }
    } catch {
      setTasks([]);
    } finally {
      setLoading(false);
    }
  }, [tenantId, view, currentDate, filterMode]);

  useEffect(() => { fetchTasks(); }, [fetchTasks]);

  // Real-time sync: when the CEO (or any other flow) creates,
  // updates, or executes a scheduled task, the backend emits a
  // Socket.IO event. Listening + refetching keeps the calendar
  // in sync without requiring the user to refresh. Covers the
  // "scheduled via chat, nothing on the calendar yet" complaint.
  useEffect(() => {
    if (!tenantId) return;
    let cleanup: (() => void) | undefined;
    try {
      const { getSocket } = require("@/lib/socket");
      const socket = getSocket();
      const refetch = () => { fetchTasks(); };
      socket.on("scheduled_task_created", refetch);
      socket.on("scheduled_task_updated", refetch);
      socket.on("scheduled_task_executed", refetch);
      socket.on("scheduled_pending_fired", refetch);
      cleanup = () => {
        socket.off("scheduled_task_created", refetch);
        socket.off("scheduled_task_updated", refetch);
        socket.off("scheduled_task_executed", refetch);
        socket.off("scheduled_pending_fired", refetch);
      };
    } catch {}
    return () => { if (cleanup) cleanup(); };
  }, [tenantId, fetchTasks]);

  function navigate(dir: number) {
    const d = new Date(currentDate);
    if (view === "month") d.setMonth(d.getMonth() + dir);
    else if (view === "week") d.setDate(d.getDate() + dir * 7);
    else d.setDate(d.getDate() + dir * 30);
    setCurrentDate(d);
  }

  async function handleCancel(taskId: string) {
    const ok = await confirm({
      title: "Cancel this scheduled task?",
      message: "It will not be executed.",
      confirmLabel: "Yes, cancel",
      cancelLabel: "Keep it",
      destructive: true,
    });
    if (!ok) return;
    try {
      await authFetch(`${API_URL}/api/schedule/${tenantId}/tasks/${taskId}/cancel`, { method: "POST" });
      setSelected(null);
      fetchTasks();
      showToast({ title: "Task cancelled", variant: "success" });
    } catch (err: any) {
      showToast({ title: "Couldn't cancel", body: err?.message, variant: "error" });
    }
  }

  async function handleApprove(taskId: string) {
    try {
      await authFetch(`${API_URL}/api/schedule/${tenantId}/tasks/${taskId}/approve`, { method: "POST" });
      setSelected(null);
      fetchTasks();
      showToast({ title: "Task approved", body: "Will run at the scheduled time.", variant: "success" });
    } catch (err: any) {
      showToast({ title: "Couldn't approve", body: err?.message, variant: "error" });
    }
  }

  async function handleExecuteNow(taskId: string) {
    const ok = await confirm({
      title: "Execute now?",
      message: "This task will run immediately instead of waiting for its scheduled time.",
      confirmLabel: "Run now",
      cancelLabel: "Cancel",
    });
    if (!ok) return;
    try {
      await authFetch(`${API_URL}/api/schedule/${tenantId}/tasks/${taskId}/execute-now`, { method: "POST" });
      setSelected(null);
      fetchTasks();
      showToast({ title: "Task executing", variant: "success" });
    } catch (err: any) {
      showToast({ title: "Couldn't execute", body: err?.message, variant: "error" });
    }
  }

  // ─── Task Card ──────────────────────────────────────────────────────────

  function TaskCard({ task, compact = false }: { task: ScheduledTask; compact?: boolean }) {
    const tt = TASK_TYPE_COLORS[task.task_type] || TASK_TYPE_COLORS.reminder_task;
    const sc = STATUS_COLORS[task.status] || STATUS_COLORS.draft;

    // Click behavior depends on source: scheduled tasks open the in-app
    // detail panel (with Approve/Cancel/Execute buttons); inbox events
    // jump to the inbox page deep-linked to that item.
    const handleClick = () => {
      if (task.source && task.source !== "scheduled" && task.href) {
        window.location.href = task.href;
      } else {
        setSelected(task);
      }
    };

    return (
      <button
        onClick={handleClick}
        className={`w-full text-left rounded-md px-2 py-1 transition-colors hover:ring-1 hover:ring-[#534AB7]/30 ${tt.bg} ${compact ? "text-[10px]" : "text-xs"}`}
      >
        <div className="flex items-center gap-1">
          <span className={`font-medium ${tt.text} truncate flex-1`}>{task.title || task.task_type}</span>
          {!compact && <span className={`text-[9px] px-1 py-0.5 rounded-full ${sc}`}>{task.status}</span>}
        </div>
        {!compact && <div className="text-[10px] text-gray-500 mt-0.5">{formatTime(task.scheduled_at)}</div>}
      </button>
    );
  }

  // ─── Month View ─────────────────────────────────────────────────────────

  function MonthView() {
    const ms = startOfMonth(currentDate);
    const ws = startOfWeek(ms);
    const weeks: Date[][] = [];
    let day = new Date(ws);
    for (let w = 0; w < 6; w++) {
      const week: Date[] = [];
      for (let d = 0; d < 7; d++) {
        week.push(new Date(day));
        day = addDays(day, 1);
      }
      weeks.push(week);
      if (day.getMonth() !== currentDate.getMonth() && day.getDay() === 0) break;
    }

    return (
      <div className="border border-[#E0DED8] rounded-xl overflow-x-auto">
        <div className="min-w-[640px]">
        <div className="grid grid-cols-7 bg-[#F8F8F6] border-b border-[#E0DED8]">
          {DAYS.map((d) => (
            <div key={d} className="text-center text-[10px] font-semibold text-[#5F5E5A] py-2 uppercase tracking-wide">{d}</div>
          ))}
        </div>
        {weeks.map((week, wi) => (
          <div key={wi} className="grid grid-cols-7 border-b border-[#E0DED8] last:border-b-0">
            {week.map((day, di) => {
              const isCurrentMonth = day.getMonth() === currentDate.getMonth();
              const isToday = isSameDay(day, new Date());
              const dayTasks = tasks.filter((t) => isSameDay(new Date(t.scheduled_at), day));

              return (
                <div
                  key={di}
                  className={`min-h-[90px] p-1.5 border-r border-[#E0DED8] last:border-r-0 ${
                    isCurrentMonth ? "bg-white" : "bg-[#FAFAF8]"
                  }`}
                >
                  <div className={`text-xs font-medium mb-1 ${
                    isToday ? "bg-[#534AB7] text-white w-6 h-6 rounded-full flex items-center justify-center" :
                    isCurrentMonth ? "text-[#2C2C2A]" : "text-[#B0AFA8]"
                  }`}>
                    {day.getDate()}
                  </div>
                  <div className="space-y-0.5">
                    {dayTasks.slice(0, 3).map((t) => (
                      <TaskCard key={t.id} task={t} compact />
                    ))}
                    {dayTasks.length > 3 && (
                      <button
                        onClick={(e) => { e.stopPropagation(); setExpandedDay({ date: day, tasks: dayTasks }); }}
                        className="text-[9px] text-[#534AB7] font-medium pl-1 hover:underline cursor-pointer w-full text-left"
                      >
                        +{dayTasks.length - 3} more
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ))}
        </div>
      </div>
    );
  }

  // ─── Week View ──────────────────────────────────────────────────────────

  function WeekView() {
    const ws = startOfWeek(currentDate);
    const days = Array.from({ length: 7 }, (_, i) => addDays(ws, i));

    return (
      <div className="border border-[#E0DED8] rounded-xl overflow-x-auto">
        <div className="min-w-[640px]">
        <div className="grid grid-cols-7 bg-[#F8F8F6] border-b border-[#E0DED8]">
          {days.map((d, i) => (
            <div key={i} className={`text-center py-2 border-r border-[#E0DED8] last:border-r-0 ${isSameDay(d, new Date()) ? "bg-[#EEEDFE]" : ""}`}>
              <div className="text-[10px] font-semibold text-[#5F5E5A] uppercase">{DAYS[d.getDay()]}</div>
              <div className={`text-lg font-bold ${isSameDay(d, new Date()) ? "text-[#534AB7]" : "text-[#2C2C2A]"}`}>{d.getDate()}</div>
            </div>
          ))}
        </div>
        <div className="grid grid-cols-7 min-h-[400px]">
          {days.map((day, i) => {
            const dayTasks = tasks.filter((t) => isSameDay(new Date(t.scheduled_at), day));
            return (
              <div key={i} className="border-r border-[#E0DED8] last:border-r-0 p-1.5 space-y-1">
                {dayTasks.map((t) => (
                  <TaskCard key={t.id} task={t} />
                ))}
                {dayTasks.length === 0 && (
                  <div className="text-[10px] text-[#6B6A65] text-center mt-4">No tasks</div>
                )}
              </div>
            );
          })}
        </div>
        </div>
      </div>
    );
  }

  // ─── Agenda View ────────────────────────────────────────────────────────

  function AgendaView() {
    const grouped: Record<string, ScheduledTask[]> = {};
    for (const t of tasks) {
      const key = new Date(t.scheduled_at).toDateString();
      if (!grouped[key]) grouped[key] = [];
      grouped[key].push(t);
    }
    const sortedDays = Object.keys(grouped).sort((a, b) => new Date(a).getTime() - new Date(b).getTime());

    return (
      <div className="space-y-4">
        {sortedDays.length === 0 && (
          <div className="text-center py-12 text-sm text-[#9E9C95]">No scheduled tasks in this period.</div>
        )}
        {sortedDays.map((dayStr) => (
          <div key={dayStr} className="bg-white rounded-xl border border-[#E0DED8] overflow-hidden">
            <div className="px-4 py-2 bg-[#F8F8F6] border-b border-[#E0DED8]">
              <span className="text-xs font-semibold text-[#2C2C2A]">{formatDate(new Date(dayStr))}</span>
              <span className="text-[10px] text-[#5F5E5A] ml-2">{grouped[dayStr].length} task{grouped[dayStr].length !== 1 ? "s" : ""}</span>
            </div>
            <div className="divide-y divide-[#F0EFEC]">
              {grouped[dayStr].map((t) => {
                const tt = TASK_TYPE_COLORS[t.task_type] || TASK_TYPE_COLORS.reminder_task;
                const sc = STATUS_COLORS[t.status] || STATUS_COLORS.draft;
                return (
                  <button
                    key={t.id}
                    onClick={() => setSelected(t)}
                    className="w-full text-left px-4 py-3 flex items-center gap-3 hover:bg-[#F8F8F6] transition-colors"
                  >
                    <div className="text-xs text-[#5F5E5A] w-16 shrink-0 font-medium">{formatTime(t.scheduled_at)}</div>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${tt.bg} ${tt.text} shrink-0`}>{tt.label}</span>
                    <span className="text-sm text-[#2C2C2A] truncate flex-1">{t.title || t.task_type}</span>
                    <span className={`text-[10px] px-1.5 py-0.5 rounded-full ${sc} shrink-0`}>{t.status}</span>
                    {t.approval_status === "pending" && (
                      <span className="text-[9px] px-1.5 py-0.5 rounded-full bg-yellow-100 text-yellow-700">Needs approval</span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    );
  }

  // ─── Detail Panel ───────────────────────────────────────────────────────

  function DetailPanel() {
    if (!selected) return null;
    const tt = TASK_TYPE_COLORS[selected.task_type] || TASK_TYPE_COLORS.reminder_task;
    const sc = STATUS_COLORS[selected.status] || STATUS_COLORS.draft;
    const scheduledDate = new Date(selected.scheduled_at);

    return (
      <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30" onClick={() => setSelected(null)}>
        <div className="bg-white rounded-xl border border-[#E0DED8] shadow-2xl w-[calc(100vw-2rem)] max-w-[460px] max-h-[80vh] overflow-y-auto mx-4" onClick={(e) => e.stopPropagation()}>
          <div className="flex items-center justify-between px-5 py-3 border-b border-[#E0DED8]">
            <h3 className="text-sm font-semibold text-[#2C2C2A]">Task Details</h3>
            <button onClick={() => setSelected(null)} className="text-[#B0AFA8] hover:text-[#2C2C2A]">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
            </button>
          </div>
          <div className="px-5 py-4 space-y-3">
            <div>
              <div className="text-lg font-semibold text-[#2C2C2A]">{selected.title || selected.task_type}</div>
              <div className="flex items-center gap-2 mt-1">
                <span className={`text-[10px] px-2 py-0.5 rounded-full ${tt.bg} ${tt.text}`}>{tt.label}</span>
                <span className={`text-[10px] px-2 py-0.5 rounded-full ${sc}`}>{selected.status}</span>
                {selected.approval_status === "pending" && (
                  <span className="text-[10px] px-2 py-0.5 rounded-full bg-yellow-100 text-yellow-700">Needs approval</span>
                )}
              </div>
            </div>
            <div className="grid grid-cols-2 gap-3 text-xs">
              <div><span className="text-[#5F5E5A]">Scheduled:</span><br /><span className="font-medium text-[#2C2C2A]">{scheduledDate.toLocaleString()}</span></div>
              <div><span className="text-[#5F5E5A]">Timezone:</span><br /><span className="font-medium text-[#2C2C2A]">{selected.timezone}</span></div>
              <div><span className="text-[#5F5E5A]">Created by:</span><br /><span className="font-medium text-[#2C2C2A]">{selected.created_by}</span></div>
              {selected.payload?.platform && (
                <div><span className="text-[#5F5E5A]">Platform:</span><br /><span className="font-medium text-[#2C2C2A]">{selected.payload.platform}</span></div>
              )}
              {selected.payload?.to && (
                <div><span className="text-[#5F5E5A]">To:</span><br /><span className="font-medium text-[#2C2C2A]">{selected.payload.to}</span></div>
              )}
              {selected.payload?.subject && (
                <div className="col-span-2"><span className="text-[#5F5E5A]">Subject:</span><br /><span className="font-medium text-[#2C2C2A]">{selected.payload.subject}</span></div>
              )}
            </div>
            {selected.payload?.text && (
              <div className="bg-[#F8F8F6] rounded-lg p-3 text-xs text-[#2C2C2A] whitespace-pre-wrap max-h-[150px] overflow-y-auto">{selected.payload.text}</div>
            )}
            <div className="flex items-center gap-2 pt-2 border-t border-[#E0DED8]">
              {/* Approve / Execute / Cancel only apply to ACTUAL scheduled
                  tasks (rows in scheduled_tasks). For inbox events from
                  the activity feed, show an "Open in Inbox" link instead.
                  We strip the "scheduled:" id prefix when calling the
                  scheduler endpoints since the backend expects raw uuids. */}
              {(!selected.source || selected.source === "scheduled") && (
                <>
                  {selected.approval_status === "pending" && (
                    <button onClick={() => handleApprove(rawTaskId(selected.id))} className="px-3 py-1.5 bg-[#1D9E75] text-white text-xs rounded-lg hover:bg-[#178a64] transition-colors">Approve</button>
                  )}
                  {["scheduled", "approved", "draft"].includes(selected.status) && (
                    <button onClick={() => handleExecuteNow(rawTaskId(selected.id))} className="px-3 py-1.5 bg-[#534AB7] text-white text-xs rounded-lg hover:bg-[#433AA0] transition-colors">Execute Now</button>
                  )}
                  {!["sent", "published", "cancelled", "failed"].includes(selected.status) && (
                    <button onClick={() => handleCancel(rawTaskId(selected.id))} className="px-3 py-1.5 bg-white text-[#D85A30] text-xs rounded-lg border border-[#D85A30] hover:bg-[#FEF2EE] transition-colors">Cancel</button>
                  )}
                </>
              )}
              {selected.source && selected.source !== "scheduled" && selected.href && (
                <a href={selected.href} className="px-3 py-1.5 bg-[#534AB7] text-white text-xs rounded-lg hover:bg-[#433AA0] transition-colors">Open in Inbox →</a>
              )}
              <button onClick={() => setSelected(null)} className="px-3 py-1.5 text-xs text-[#5F5E5A] hover:text-[#2C2C2A] ml-auto">Close</button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // ─── Render ─────────────────────────────────────────────────────────────

  const headerTitle = view === "month"
    ? `${MONTHS[currentDate.getMonth()]} ${currentDate.getFullYear()}`
    : view === "week"
    ? `Week of ${formatDate(startOfWeek(currentDate))}`
    : "Upcoming Tasks";

  return (
    <div className="max-w-screen-xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">Calendar</h1>
          <p className="text-xs text-[#5F5E5A] mt-0.5">Scheduled emails, posts, campaigns, and reminders</p>
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => setCurrentDate(new Date())} className="text-xs px-3 py-1.5 rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors">Today</button>
        </div>
      </div>

      {/* Toolbar */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <button onClick={() => navigate(-1)} className="p-1.5 rounded-lg hover:bg-[#F8F8F6] text-[#5F5E5A] transition-colors">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" /></svg>
          </button>
          <h2 className="text-base font-semibold text-[#2C2C2A] min-w-[200px] text-center">{headerTitle}</h2>
          <button onClick={() => navigate(1)} className="p-1.5 rounded-lg hover:bg-[#F8F8F6] text-[#5F5E5A] transition-colors">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" /></svg>
          </button>
        </div>
        <div className="flex items-center gap-3">
          {/* Filter mode: All activity (drafts+sent+scheduled) vs Scheduled only */}
          <div className="flex items-center gap-1 bg-[#F8F8F6] rounded-lg p-0.5">
            {([
              { key: "all" as FilterMode, label: "All activity" },
              { key: "scheduled" as FilterMode, label: "Scheduled" },
            ]).map((opt) => (
              <button
                key={opt.key}
                onClick={() => setFilterMode(opt.key)}
                className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                  filterMode === opt.key ? "bg-white text-[#534AB7] shadow-sm" : "text-[#5F5E5A] hover:text-[#2C2C2A]"
                }`}
                title={opt.key === "all" ? "Show drafts, sent items, and scheduled tasks" : "Show only items explicitly queued for execution"}
              >
                {opt.label}
              </button>
            ))}
          </div>
          {/* View mode: month / week / agenda */}
          <div className="flex items-center gap-1 bg-[#F8F8F6] rounded-lg p-0.5">
            {(["month", "week", "agenda"] as ViewMode[]).map((v) => (
              <button
                key={v}
                onClick={() => setView(v)}
                className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
                  view === v ? "bg-white text-[#534AB7] shadow-sm" : "text-[#5F5E5A] hover:text-[#2C2C2A]"
                }`}
              >
                {v.charAt(0).toUpperCase() + v.slice(1)}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Activity legend (only when in 'all' mode and we have events) */}
      {filterMode === "all" && tasks.length > 0 && (
        <div className="flex items-center gap-3 text-[10px] text-[#5F5E5A] flex-wrap">
          <span className="font-medium">Showing:</span>
          {([
            { key: "scheduled", label: "Scheduled", color: "bg-blue-400" },
            { key: "inbox_draft", label: "Drafts", color: "bg-gray-400" },
            { key: "inbox_sent", label: "Sent", color: "bg-emerald-400" },
          ] as const).map((legend) => {
            const count = tasks.filter((t) => t.source === legend.key).length;
            if (count === 0) return null;
            return (
              <span key={legend.key} className="inline-flex items-center gap-1.5">
                <span className={`w-2 h-2 rounded-full ${legend.color}`} />
                {count} {legend.label.toLowerCase()}
              </span>
            );
          })}
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="flex items-center justify-center py-8">
          <div className="w-6 h-6 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
        </div>
      )}

      {/* Views */}
      {!loading && view === "month" && <MonthView />}
      {!loading && view === "week" && <WeekView />}
      {!loading && view === "agenda" && <AgendaView />}

      {/* Detail modal */}
      <DetailPanel />

      {/* Expanded day modal: shown when user clicks "+N more" on a day cell */}
      {expandedDay && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/40 backdrop-blur-sm"
          onClick={() => setExpandedDay(null)}
        >
          <div
            className="bg-white rounded-xl border border-[#E0DED8] shadow-2xl w-[480px] max-w-[calc(100vw-32px)] max-h-[80vh] overflow-hidden flex flex-col"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-5 py-3 border-b border-[#E0DED8]">
              <h3 className="text-sm font-semibold text-[#2C2C2A]">
                {expandedDay.tasks.length} task{expandedDay.tasks.length === 1 ? "" : "s"} on {expandedDay.date.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" })}
              </h3>
              <button onClick={() => setExpandedDay(null)} className="text-[#6B6A65] hover:text-[#2C2C2A]">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div className="flex-1 overflow-auto px-5 py-4 space-y-2">
              {expandedDay.tasks.map((t) => (
                <div key={t.id} onClick={() => { setSelected(t); setExpandedDay(null); }}>
                  <TaskCard task={t} />
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
