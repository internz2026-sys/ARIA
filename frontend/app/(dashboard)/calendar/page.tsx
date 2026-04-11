"use client";

import React, { useState, useEffect, useCallback } from "react";
import { API_URL, authFetch } from "@/lib/api";

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
}

type ViewMode = "month" | "week" | "agenda";

// Keys must match the backend's task_type values from
// backend/services/scheduler.py executor switch.
// Both with and without `_task` suffix are listed because earlier code
// used the suffixed form -- support both so old rows still render.
const TASK_TYPE_COLORS: Record<string, { bg: string; text: string; label: string }> = {
  send_email: { bg: "bg-blue-100", text: "text-blue-700", label: "Email" },
  publish_post: { bg: "bg-purple-100", text: "text-purple-700", label: "Post" },
  publish_campaign: { bg: "bg-orange-100", text: "text-orange-700", label: "Campaign" },
  follow_up: { bg: "bg-emerald-100", text: "text-emerald-700", label: "Follow-up" },
  follow_up_task: { bg: "bg-emerald-100", text: "text-emerald-700", label: "Follow-up" },
  reminder: { bg: "bg-amber-100", text: "text-amber-700", label: "Reminder" },
  reminder_task: { bg: "bg-amber-100", text: "text-amber-700", label: "Reminder" },
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
  const [view, setView] = useState<ViewMode>("month");
  const [currentDate, setCurrentDate] = useState(new Date());
  const [tasks, setTasks] = useState<ScheduledTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [selected, setSelected] = useState<ScheduledTask | null>(null);
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

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
      const res = await authFetch(`${API_URL}/api/schedule/${tenantId}/calendar?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`);
      const data = await res.json();
      setTasks(data.tasks || []);
    } catch {
      setTasks([]);
    } finally {
      setLoading(false);
    }
  }, [tenantId, view, currentDate]);

  useEffect(() => { fetchTasks(); }, [fetchTasks]);

  function navigate(dir: number) {
    const d = new Date(currentDate);
    if (view === "month") d.setMonth(d.getMonth() + dir);
    else if (view === "week") d.setDate(d.getDate() + dir * 7);
    else d.setDate(d.getDate() + dir * 30);
    setCurrentDate(d);
  }

  async function handleCancel(taskId: string) {
    if (!confirm("Cancel this scheduled task?")) return;
    await authFetch(`${API_URL}/api/schedule/${tenantId}/tasks/${taskId}/cancel`, { method: "POST" });
    setSelected(null);
    fetchTasks();
  }

  async function handleApprove(taskId: string) {
    await authFetch(`${API_URL}/api/schedule/${tenantId}/tasks/${taskId}/approve`, { method: "POST" });
    setSelected(null);
    fetchTasks();
  }

  async function handleExecuteNow(taskId: string) {
    if (!confirm("Execute this task immediately?")) return;
    await authFetch(`${API_URL}/api/schedule/${tenantId}/tasks/${taskId}/execute-now`, { method: "POST" });
    setSelected(null);
    fetchTasks();
  }

  // ─── Task Card ──────────────────────────────────────────────────────────

  function TaskCard({ task, compact = false }: { task: ScheduledTask; compact?: boolean }) {
    const tt = TASK_TYPE_COLORS[task.task_type] || TASK_TYPE_COLORS.reminder_task;
    const sc = STATUS_COLORS[task.status] || STATUS_COLORS.draft;

    return (
      <button
        onClick={() => setSelected(task)}
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
      <div className="border border-[#E0DED8] rounded-xl overflow-hidden">
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
                      <div className="text-[9px] text-[#534AB7] font-medium pl-1">+{dayTasks.length - 3} more</div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    );
  }

  // ─── Week View ──────────────────────────────────────────────────────────

  function WeekView() {
    const ws = startOfWeek(currentDate);
    const days = Array.from({ length: 7 }, (_, i) => addDays(ws, i));

    return (
      <div className="border border-[#E0DED8] rounded-xl overflow-hidden">
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
                  <div className="text-[10px] text-[#B0AFA8] text-center mt-4">No tasks</div>
                )}
              </div>
            );
          })}
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
        <div className="bg-white rounded-xl border border-[#E0DED8] shadow-2xl w-[460px] max-h-[80vh] overflow-y-auto" onClick={(e) => e.stopPropagation()}>
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
              {selected.approval_status === "pending" && (
                <button onClick={() => handleApprove(selected.id)} className="px-3 py-1.5 bg-[#1D9E75] text-white text-xs rounded-lg hover:bg-[#178a64] transition-colors">Approve</button>
              )}
              {["scheduled", "approved", "draft"].includes(selected.status) && (
                <button onClick={() => handleExecuteNow(selected.id)} className="px-3 py-1.5 bg-[#534AB7] text-white text-xs rounded-lg hover:bg-[#433AA0] transition-colors">Execute Now</button>
              )}
              {!["sent", "published", "cancelled", "failed"].includes(selected.status) && (
                <button onClick={() => handleCancel(selected.id)} className="px-3 py-1.5 bg-white text-[#D85A30] text-xs rounded-lg border border-[#D85A30] hover:bg-[#FEF2EE] transition-colors">Cancel</button>
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
    <div className="max-w-[1100px] space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-[#2C2C2A]">Calendar</h1>
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
    </div>
  );
}
