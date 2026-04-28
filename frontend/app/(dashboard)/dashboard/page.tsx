"use client";

import React, { useState, useEffect, useRef } from "react";
import { supabase } from "@/lib/supabase";
import { API_URL, authFetch } from "@/lib/api";
import { AGENT_DEFS } from "@/lib/agent-config";
import { PRIORITY_STYLES } from "@/lib/task-config";

function getGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

function getDateString(): string {
  return new Date().toLocaleDateString("en-US", { weekday: "long", year: "numeric", month: "long", day: "numeric" });
}

type Priority = "low" | "medium" | "high";
type Column = "backlog" | "todo" | "in_progress" | "done";

interface Task {
  id: string;
  title: string;
  agent: string;
  priority: Priority;
  column: Column;
  scheduled?: string;
}

const COLUMNS: { key: Column; label: string; color: string }[] = [
  { key: "backlog", label: "Backlog", color: "#5F5E5A" },
  { key: "todo", label: "To Do", color: "#534AB7" },
  { key: "in_progress", label: "In Progress", color: "#BA7517" },
  { key: "done", label: "Done", color: "#1D9E75" },
];

const priorityStyles = PRIORITY_STYLES;

let _idCounter = 0;
function genId() { return `task_${++_idCounter}_${Date.now()}`; }

interface BusinessContext {
  business_name: string | null;
  product_name: string | null;
  product_description: string | null;
  positioning: string | null;
  active_agents: string[];
  channels: string[];
  action_plan_30: string | null;
  messaging_pillars: string[];
  onboarding_status: string;
  skipped_fields: string[];
}

const SKIPPED_FIELD_INFO: Record<string, { label: string; desc: string; icon: string }> = {
  product: { label: "Product Details", desc: "Tell ARIA what your product does and who it's for", icon: "M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4" },
  icp: { label: "Target Audience", desc: "Define your ideal customer profile and pain points", icon: "M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" },
  "product.value_props": { label: "Value Proposition", desc: "What makes your product uniquely valuable?", icon: "M9 12l2 2 4-4M7.835 4.697a3.42 3.42 0 001.946-.806 3.42 3.42 0 014.438 0 3.42 3.42 0 001.946.806 3.42 3.42 0 013.138 3.138 3.42 3.42 0 00.806 1.946 3.42 3.42 0 010 4.438 3.42 3.42 0 00-.806 1.946 3.42 3.42 0 01-3.138 3.138 3.42 3.42 0 00-1.946.806 3.42 3.42 0 01-4.438 0 3.42 3.42 0 00-1.946-.806 3.42 3.42 0 01-3.138-3.138 3.42 3.42 0 00-.806-1.946 3.42 3.42 0 010-4.438 3.42 3.42 0 00.806-1.946 3.42 3.42 0 013.138-3.138z" },
  "product.differentiators": { label: "Differentiators", desc: "How do you stand out from competitors?", icon: "M13 10V3L4 14h7v7l9-11h-7z" },
  "product.competitors": { label: "Competitors", desc: "Who are you competing with?", icon: "M3 6l3 1m0 0l-3 9a5.002 5.002 0 006.001 0M6 7l3 9M6 7l6-2m6 2l3-1m-3 1l-3 9a5.002 5.002 0 006.001 0M18 7l3 9m-3-9l-6-2m0-2v2m0 16V5m0 16H9m3 0h3" },
  "gtm_playbook.competitor_differentiation": { label: "Competitor Strategy", desc: "How to position against competitors", icon: "M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" },
  "gtm_playbook.kpis": { label: "Marketing Goals", desc: "Set KPIs and success metrics", icon: "M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" },
  "gtm_playbook.action_plan_30": { label: "30-Day Plan", desc: "What to focus on in the first month", icon: "M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" },
  "gtm_playbook.action_plan_60": { label: "60-Day Plan", desc: "Growth actions for months 1-2", icon: "M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" },
  "gtm_playbook.action_plan_90": { label: "90-Day Plan", desc: "Scaling actions for months 2-3", icon: "M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" },
  brand_voice: { label: "Brand Voice", desc: "Define your tone, style, and messaging guidelines", icon: "M7 8h10M7 12h4m1 8l-4-4H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-3l-4 4z" },
  channels: { label: "Marketing Channels", desc: "Where to reach your audience", icon: "M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9" },
  "gtm_playbook.channel_strategy": { label: "Channel Strategy", desc: "Prioritize and plan your channel approach", icon: "M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" },
};

export default function DashboardPage() {
  const [firstName, setFirstName] = useState("");
  const [kpis, setKpis] = useState({ content_published: { value: 0, delta: 0, delta_pct: 0 }, emails_sent: { value: 0, open_rate: 0, click_rate: 0 }, social_engagement: { value: 0, delta_pct: 0 }, ad_spend: { value: 0, roas: 0 } });
  const [paperclipConnected, setPaperclipConnected] = useState(false);
  const [biz, setBiz] = useState<BusinessContext | null>(null);
  const [tenantId, setTenantId] = useState("");

  // Board state
  const [tasks, setTasks] = useState<Task[]>([]);
  const [showAddModal, setShowAddModal] = useState(false);
  const [addToColumn, setAddToColumn] = useState<Column>("todo");
  const [newTask, setNewTask] = useState({ title: "", agent: "ceo", priority: "medium" as Priority, scheduled: "" });
  const [draggedId, setDraggedId] = useState<string | null>(null);
  const [viewingTask, setViewingTask] = useState<Task | null>(null);
  const [triaging, setTriaging] = useState(false);
  const [triageReason, setTriageReason] = useState("");
  const triageTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);

  async function autoTriage(title: string) {
    if (!title.trim() || title.trim().length < 5) return;
    setTriaging(true);
    setTriageReason("");
    try {
      const res = await authFetch(`${API_URL}/api/ceo/triage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.trim() }),
      });
      const data = await res.json();
      setNewTask(prev => ({ ...prev, agent: data.agent || "ceo", priority: data.priority || "medium" }));
      setAddToColumn(data.column || "todo");
      setTriageReason(data.reason || "");
    } catch {
      setTriageReason("CEO unavailable — using defaults");
    }
    setTriaging(false);
  }

  function handleTitleChange(val: string) {
    setNewTask(prev => ({ ...prev, title: val }));
    setTriageReason("");
    if (triageTimeout.current) clearTimeout(triageTimeout.current);
    if (val.trim().length >= 5) {
      triageTimeout.current = setTimeout(() => autoTriage(val), 800);
    }
  }

  useEffect(() => {
    const tid = localStorage.getItem("aria_tenant_id") || "demo";
    setTenantId(tid);

    supabase.auth.getSession().then(({ data: { session } }) => {
      if (session?.user) {
        const meta = session.user.user_metadata;
        const fullName = meta?.full_name || meta?.name || session.user.email?.split("@")[0] || "there";
        setFirstName(fullName.split(" ")[0]);
      }
    });

    authFetch(`${API_URL}/api/dashboard/${tid}/stats`).then(r => r.json()).then(d => d.kpis && setKpis(d.kpis)).catch(() => {});
    authFetch(`${API_URL}/api/paperclip/status`).then(r => r.json()).then(d => setPaperclipConnected(d.connected)).catch(() => {});

    if (tid !== "demo") {
      authFetch(`${API_URL}/api/dashboard/${tid}/config`)
        .then(r => r.json())
        .then(d => { if (d.business_name) setBiz(d); })
        .catch(() => {});
    }

    // Load tasks from API
    authFetch(`${API_URL}/api/tasks/${tid}`)
      .then(r => r.json())
      .then(data => {
        if (data.tasks) {
          setTasks(data.tasks.map((t: any) => ({
            id: t.id,
            title: t.task,
            agent: t.agent,
            priority: t.priority || "medium",
            column: (t.status === "to_do" ? "todo" : t.status) as Column,
            scheduled: undefined,
          })));
        }
      })
      .catch(() => {});
  }, []);

  function addTask() {
    if (!newTask.title.trim()) return;
    setTasks(prev => [...prev, { id: genId(), title: newTask.title.trim(), agent: newTask.agent, priority: newTask.priority, column: addToColumn, scheduled: newTask.scheduled || undefined }]);
    setNewTask({ title: "", agent: "ceo", priority: "medium", scheduled: "" });
    setShowAddModal(false);
  }

  function deleteTask(id: string) {
    setTasks(prev => prev.filter(t => t.id !== id));
    authFetch(`${API_URL}/api/tasks/${id}`, { method: "DELETE" }).catch(() => {});
  }

  function moveTask(id: string, to: Column) {
    setTasks(prev => prev.map(t => t.id === id ? { ...t, column: to } : t));
    const apiStatus = to === "todo" ? "to_do" : to;
    authFetch(`${API_URL}/api/tasks/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: apiStatus }),
    }).catch(() => {});
  }

  function handleDragStart(id: string) { setDraggedId(id); }
  function handleDragOver(e: React.DragEvent) { e.preventDefault(); }
  function handleDrop(col: Column) {
    if (draggedId) { moveTask(draggedId, col); setDraggedId(null); }
  }

  async function delegateTask(task: Task) {
    moveTask(task.id, "in_progress");
    try {
      await authFetch(`${API_URL}/api/agents/${tenantId}/${task.agent}/run`, { method: "POST" });
      moveTask(task.id, "done");
    } catch {
      moveTask(task.id, "todo");
    }
  }

  return (
    <div className="space-y-6 max-w-screen-2xl mx-auto">
      {/* Greeting */}
      <div>
        <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">{getGreeting()}, {firstName || "..."}</h1>
        <p className="text-sm text-[#5F5E5A] mt-0.5">
          {getDateString()} &middot;{" "}
          <span className="text-[#534AB7] font-medium">
            {biz ? `${biz.active_agents.length} marketing agents` : "5 marketing agents"} {paperclipConnected ? "connected" : "offline"}
          </span>
        </p>
        {biz && (
          <p className="text-sm text-[#5F5E5A] mt-1">
            Managing marketing for <span className="font-semibold text-[#2C2C2A]">{biz.product_name || biz.business_name}</span>
          </p>
        )}
      </div>

      {/* Business Context Card */}
      {biz && biz.positioning && (
        <div className="bg-gradient-to-r from-[#EEEDFE] to-[#F8F8F6] rounded-xl border border-[#534AB7]/15 p-5">
          <div className="flex items-start gap-3">
            <div className="w-8 h-8 rounded-lg bg-[#534AB7] flex items-center justify-center shrink-0">
              <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
              </svg>
            </div>
            <div className="flex-1 min-w-0">
              <h3 className="text-sm font-semibold text-[#2C2C2A]">Your GTM Positioning</h3>
              <p className="text-sm text-[#5F5E5A] mt-1">{biz.positioning}</p>
              {biz.action_plan_30 && (
                <div className="mt-3 pt-3 border-t border-[#534AB7]/10">
                  <p className="text-xs font-semibold text-[#534AB7] mb-1">30-Day Focus</p>
                  <p className="text-xs text-[#5F5E5A]">{biz.action_plan_30}</p>
                </div>
              )}
              {biz.channels.length > 0 && (
                <div className="flex items-center gap-1.5 mt-3 flex-wrap">
                  {biz.channels.map(ch => (
                    <span key={ch} className="text-[10px] font-medium px-2 py-0.5 rounded-full bg-white border border-[#E0DED8] text-[#5F5E5A]">{ch}</span>
                  ))}
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Skipped Fields Banner */}
      {biz && biz.skipped_fields && biz.skipped_fields.length > 0 && (
        <div className="bg-[#FDF3E7] rounded-xl border border-[#BA7517]/20 p-5">
          <div className="flex items-start gap-3 mb-4">
            <div className="w-8 h-8 rounded-lg bg-[#BA7517] flex items-center justify-center shrink-0">
              <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
              </svg>
            </div>
            <div>
              <h3 className="text-sm font-semibold text-[#2C2C2A]">Complete your profile</h3>
              <p className="text-xs text-[#5F5E5A] mt-0.5">
                You skipped {biz.skipped_fields.length} section{biz.skipped_fields.length > 1 ? "s" : ""} during onboarding. Fill them in so ARIA can create better content for you.
              </p>
            </div>
          </div>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
            {biz.skipped_fields.map(field => {
              const info = SKIPPED_FIELD_INFO[field];
              if (!info) return null;
              return (
                <a
                  key={field}
                  href="/welcome"
                  className="flex items-center gap-3 p-3 bg-white rounded-lg border border-[#E0DED8] hover:border-[#534AB7]/40 hover:shadow-sm transition-all group"
                >
                  <div className="w-8 h-8 rounded-lg bg-[#F8F8F6] flex items-center justify-center shrink-0 group-hover:bg-[#EEEDFE] transition-colors">
                    <svg className="w-4 h-4 text-[#5F5E5A] group-hover:text-[#534AB7] transition-colors" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d={info.icon} />
                    </svg>
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-xs font-semibold text-[#2C2C2A] group-hover:text-[#534AB7] transition-colors">{info.label}</p>
                    <p className="text-[10px] text-[#5F5E5A] truncate">{info.desc}</p>
                  </div>
                </a>
              );
            })}
          </div>
        </div>
      )}

      {/* KPI Cards moved to /analytics (Content Published, Emails
          Sent, Social Engagement, Ad Spend). Dashboard keeps the
          activity / status / delegation surface; Analytics owns the
          quantitative performance view. */}

      {/* ─── Project Management Board ─── */}
      <div className="bg-white rounded-xl border border-[#E0DED8]">
        <div className="px-5 py-4 border-b border-[#E0DED8] flex items-center justify-between">
          <div>
            <h2 className="text-base font-semibold text-[#2C2C2A]">Project Management</h2>
            <p className="text-xs text-[#5F5E5A] mt-0.5">Drag tasks between columns or delegate to agents</p>
          </div>
          <button
            onClick={() => { setAddToColumn("todo"); setShowAddModal(true); }}
            className="flex items-center gap-1.5 px-3 py-2 bg-[#534AB7] text-white rounded-lg text-xs font-medium hover:bg-[#433AA0] transition-colors"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
            </svg>
            Add task
          </button>
        </div>

        {/* Kanban columns */}
        <div className="grid grid-cols-1 md:grid-cols-4 gap-0 divide-x divide-[#E0DED8] min-h-[320px]">
          {COLUMNS.map((col) => {
            const colTasks = tasks.filter(t => t.column === col.key);
            return (
              <div
                key={col.key}
                className="flex flex-col"
                onDragOver={handleDragOver}
                onDrop={() => handleDrop(col.key)}
              >
                {/* Column header */}
                <div className="px-4 py-3 border-b border-[#E0DED8] flex items-center justify-between bg-[#F8F8F6]">
                  <div className="flex items-center gap-2">
                    <div className="w-2 h-2 rounded-full" style={{ backgroundColor: col.color }} />
                    <span className="text-xs font-semibold text-[#2C2C2A]">{col.label}</span>
                    <span className="text-[10px] font-medium px-1.5 py-0.5 rounded-full bg-white text-[#5F5E5A] border border-[#E0DED8]">{colTasks.length}</span>
                  </div>
                  <button
                    onClick={() => { setAddToColumn(col.key); setShowAddModal(true); }}
                    className="text-[#5F5E5A] hover:text-[#534AB7] transition-colors"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" />
                    </svg>
                  </button>
                </div>

                {/* Cards */}
                <div className="flex-1 p-2 space-y-2 overflow-y-auto max-h-[400px]">
                  {colTasks.length === 0 && (
                    <div className="text-center py-8 text-[10px] text-[#E0DED8]">
                      Drop tasks here
                    </div>
                  )}
                  {colTasks.map((task) => {
                    const agentInfo = AGENT_DEFS.find(a => a.slug === task.agent) || AGENT_DEFS[0];
                    const ps = priorityStyles[task.priority];
                    // Truncate to first sentence or 60 chars
                    const shortTitle = task.title.length > 60
                      ? task.title.slice(0, 60).replace(/\s+\S*$/, "") + "..."
                      : task.title.split(/[.!]\s/)[0];
                    return (
                      <div
                        key={task.id}
                        draggable
                        onDragStart={() => handleDragStart(task.id)}
                        onClick={() => setViewingTask(task)}
                        className="bg-white rounded-lg border border-[#E0DED8] px-3 py-2 shadow-sm hover:shadow-md transition-shadow cursor-grab active:cursor-grabbing group"
                        title={task.title}
                      >
                        {/* Compact: one-line title + agent dot. Whole card
                            is clickable now (not just the eye icon which
                            was opacity-0 group-hover-only and invisible on
                            touch devices). title attr also gives a hover
                            tooltip showing the full text on desktop. */}
                        <div className="flex items-center gap-2">
                          <div className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: agentInfo.color }} title={agentInfo.name} />
                          <p className="text-xs font-medium text-[#2C2C2A] truncate flex-1" title={task.title}>{shortTitle}</p>
                          <span className="text-[9px] font-medium px-1 py-0.5 rounded shrink-0" style={{ color: ps.color, backgroundColor: ps.bg }}>{ps.label}</span>
                          {/* View button */}
                          <button
                            onClick={(e) => { e.stopPropagation(); setViewingTask(task); }}
                            className="text-[#B0AFA8] hover:text-[#534AB7] shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
                            title="View details"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M2.036 12.322a1.012 1.012 0 010-.639C3.423 7.51 7.36 4.5 12 4.5c4.64 0 8.577 3.007 9.963 7.178.07.207.07.431 0 .639C20.577 16.49 16.64 19.5 12 19.5c-4.64 0-8.577-3.007-9.963-7.178z" /><path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" /></svg>
                          </button>
                          {/* Delete */}
                          <button
                            onClick={(e) => { e.stopPropagation(); deleteTask(task.id); }}
                            className="text-[#E0DED8] hover:text-[#D85A30] shrink-0 opacity-0 group-hover:opacity-100 transition-opacity"
                            title="Delete"
                          >
                            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                          </button>
                        </div>
                      </div>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* ─── Add Task Modal ─── */}
      {showAddModal && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => setShowAddModal(false)}>
          <div className="bg-white rounded-2xl border border-[#E0DED8] shadow-xl w-full max-w-md p-6" onClick={e => e.stopPropagation()}>
            <h3 className="text-lg font-semibold text-[#2C2C2A] mb-1">New Task</h3>
            <p className="text-xs text-[#5F5E5A] mb-4">Type a task and the CEO agent will auto-classify it</p>

            <div className="space-y-4">
              {/* Title input with triage indicator */}
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Task description</label>
                <div className="relative">
                  <input
                    type="text"
                    value={newTask.title}
                    onChange={e => handleTitleChange(e.target.value)}
                    placeholder="e.g. Write blog post about API best practices"
                    autoFocus
                    className="w-full px-3 py-2.5 pr-10 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] placeholder:text-[#6B6A65] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7]"
                    onKeyDown={e => e.key === "Enter" && !triaging && addTask()}
                  />
                  {triaging && (
                    <div className="absolute right-3 top-1/2 -translate-y-1/2">
                      <div className="w-4 h-4 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
                    </div>
                  )}
                </div>
              </div>

              {/* CEO triage result */}
              {triageReason && (
                <div className="flex items-start gap-2 p-3 rounded-lg bg-[#EEEDFE]/50 border border-[#534AB7]/15">
                  <div className="w-5 h-5 rounded-full bg-[#534AB7] flex items-center justify-center shrink-0 mt-0.5">
                    <span className="text-white text-[9px] font-bold">AI</span>
                  </div>
                  <div>
                    <p className="text-[11px] font-semibold text-[#534AB7]">CEO Recommendation</p>
                    <p className="text-xs text-[#5F5E5A] mt-0.5">{triageReason}</p>
                  </div>
                </div>
              )}
              {triaging && (
                <div className="flex items-center gap-2 p-3 rounded-lg bg-[#F8F8F6] border border-[#E0DED8]">
                  <div className="w-4 h-4 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
                  <p className="text-xs text-[#5F5E5A]">CEO is analyzing your task...</p>
                </div>
              )}

              {/* Agent assignment (auto-filled by CEO, editable) */}
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">
                  Assign to agent
                  {triageReason && <span className="text-[#534AB7] ml-1">(auto-assigned by CEO)</span>}
                </label>
                <div className="grid grid-cols-5 gap-1.5">
                  {AGENT_DEFS.map(a => (
                    <button
                      key={a.slug}
                      onClick={() => setNewTask(prev => ({ ...prev, agent: a.slug }))}
                      className={`py-2 px-1 rounded-lg text-[10px] font-medium text-center transition-all border ${
                        newTask.agent === a.slug
                          ? "border-[#534AB7] bg-[#EEEDFE] text-[#534AB7] ring-2 ring-[#534AB7]/20"
                          : "border-[#E0DED8] text-[#5F5E5A] hover:border-[#534AB7]/40"
                      }`}
                    >
                      <div className="w-3 h-3 rounded-full mx-auto mb-1" style={{ backgroundColor: a.color }} />
                      {a.name.replace("ARIA ", "")}
                    </button>
                  ))}
                </div>
              </div>

              <div className="grid grid-cols-2 gap-4">
                {/* Priority (auto-filled by CEO, editable) */}
                <div>
                  <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">
                    Priority
                    {triageReason && <span className="text-[#534AB7] ml-1">(auto)</span>}
                  </label>
                  <div className="flex gap-1.5">
                    {(["low", "medium", "high"] as Priority[]).map(p => {
                      const ps = priorityStyles[p];
                      return (
                        <button
                          key={p}
                          onClick={() => setNewTask(prev => ({ ...prev, priority: p }))}
                          className={`flex-1 py-2 rounded-lg text-xs font-medium transition-all border ${
                            newTask.priority === p ? "ring-2 ring-current/20" : "border-[#E0DED8]"
                          }`}
                          style={{ color: newTask.priority === p ? ps.color : "#5F5E5A", backgroundColor: newTask.priority === p ? ps.bg : "white", borderColor: newTask.priority === p ? ps.color : undefined }}
                        >
                          {ps.label}
                        </button>
                      );
                    })}
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Schedule (optional)</label>
                  <input
                    type="date"
                    value={newTask.scheduled}
                    onChange={e => setNewTask(prev => ({ ...prev, scheduled: e.target.value }))}
                    className="w-full px-3 py-2 bg-white border border-[#E0DED8] rounded-lg text-xs text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7]"
                  />
                </div>
              </div>

              {/* Column (auto-filled by CEO, editable) */}
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">
                  Column
                  {triageReason && <span className="text-[#534AB7] ml-1">(auto-sorted by CEO)</span>}
                </label>
                <div className="flex gap-1.5">
                  {COLUMNS.map(c => (
                    <button
                      key={c.key}
                      onClick={() => setAddToColumn(c.key)}
                      className={`flex-1 py-2 rounded-lg text-xs font-medium transition-all border ${
                        addToColumn === c.key
                          ? "border-[#534AB7] bg-[#EEEDFE] text-[#534AB7]"
                          : "border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6]"
                      }`}
                    >
                      {c.label}
                    </button>
                  ))}
                </div>
              </div>
            </div>

            <div className="flex items-center gap-2 mt-6">
              <button
                onClick={addTask}
                disabled={!newTask.title.trim() || triaging}
                className="flex-1 py-2.5 bg-[#534AB7] text-white rounded-lg text-sm font-medium hover:bg-[#433AA0] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Add task
              </button>
              <button
                onClick={() => {
                  const t: Task = { id: genId(), title: newTask.title.trim(), agent: newTask.agent, priority: newTask.priority, column: "todo", scheduled: newTask.scheduled || undefined };
                  setTasks(prev => [...prev, t]);
                  delegateTask(t);
                  setShowAddModal(false);
                  setNewTask({ title: "", agent: "ceo", priority: "medium", scheduled: "" });
                  setTriageReason("");
                }}
                disabled={!newTask.title.trim() || triaging}
                className="flex-1 py-2.5 border border-[#534AB7] text-[#534AB7] rounded-lg text-sm font-medium hover:bg-[#EEEDFE] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Delegate now
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ─── Task Detail Modal ─── */}
      {viewingTask && (() => {
        const agentInfo = AGENT_DEFS.find(a => a.slug === viewingTask.agent) || AGENT_DEFS[0];
        const ps = priorityStyles[viewingTask.priority];
        const colLabel = COLUMNS.find(c => c.key === viewingTask.column)?.label || viewingTask.column;
        return (
          <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => setViewingTask(null)}>
            <div className="bg-white rounded-2xl border border-[#E0DED8] shadow-xl w-full max-w-lg p-6" onClick={e => e.stopPropagation()}>
              <div className="flex items-start justify-between mb-4">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-semibold px-2 py-1 rounded text-white" style={{ backgroundColor: agentInfo.color }}>{agentInfo.name}</span>
                  <span className="text-xs font-medium px-2 py-0.5 rounded-full" style={{ color: ps.color, backgroundColor: ps.bg }}>{ps.label}</span>
                  <span className="text-xs text-[#5F5E5A] bg-[#F8F8F6] px-2 py-0.5 rounded-full">{colLabel}</span>
                </div>
                <button onClick={() => setViewingTask(null)} className="text-[#5F5E5A] hover:text-[#2C2C2A] transition">
                  <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                </button>
              </div>
              <p className="text-sm text-[#2C2C2A] leading-relaxed whitespace-pre-wrap">{viewingTask.title}</p>
              <div className="flex items-center gap-2 mt-5 pt-4 border-t border-[#E0DED8]">
                {viewingTask.column !== "done" && (
                  <button
                    onClick={() => {
                      const nextCol = viewingTask.column === "backlog" ? "todo" : viewingTask.column === "todo" ? "in_progress" : "done";
                      moveTask(viewingTask.id, nextCol as Column);
                      setViewingTask({ ...viewingTask, column: nextCol as Column });
                    }}
                    className="flex-1 py-2 bg-[#534AB7] text-white rounded-lg text-xs font-medium hover:bg-[#433AA0] transition"
                  >
                    Move to {viewingTask.column === "backlog" ? "To Do" : viewingTask.column === "todo" ? "In Progress" : "Done"}
                  </button>
                )}
                <button
                  onClick={() => { deleteTask(viewingTask.id); setViewingTask(null); }}
                  className="py-2 px-4 border border-[#E0DED8] text-[#D85A30] rounded-lg text-xs font-medium hover:bg-[#FEF2EE] transition"
                >
                  Delete
                </button>
              </div>
            </div>
          </div>
        );
      })()}

      {/* Agent Status */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-4">
        <div className="lg:col-span-2 bg-white rounded-xl border border-[#E0DED8]">
          <div className="px-5 py-4 border-b border-[#E0DED8]">
            <h2 className="text-base font-semibold text-[#2C2C2A]">Agent Status</h2>
          </div>
          <div className="divide-y divide-[#E0DED8]">
            {AGENT_DEFS.map((agent) => {
              const isActive = !biz || biz.active_agents.includes(agent.slug);
              return (
                <div key={agent.slug} className={`px-5 py-3.5 flex items-center justify-between ${!isActive ? "opacity-40" : ""}`}>
                  <div className="flex items-center gap-3">
                    <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: agent.color }} />
                    <span className="text-sm font-medium text-[#2C2C2A]">{agent.name}</span>
                  </div>
                  <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${isActive ? "text-[#1D9E75] bg-[#E6F7F0]" : "text-[#5F5E5A] bg-[#F8F8F6]"}`}>
                    {isActive ? "Ready" : "Inactive"}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        <div className="lg:col-span-3 bg-white rounded-xl border border-[#E0DED8]">
          <div className="px-5 py-4 border-b border-[#E0DED8]">
            <h2 className="text-base font-semibold text-[#2C2C2A]">Getting Started</h2>
          </div>
          <div className="p-5 space-y-3">
            {[
              {
                label: "Complete onboarding",
                desc: "Tell ARIA about your product and target audience",
                href: "/welcome",
                done: !!biz,
              },
              {
                label: "Review your GTM playbook",
                desc: "CEO agent creates your go-to-market strategy",
                href: "/dashboard",
                done: !!biz?.positioning,
              },
              {
                // Was: "Run your first agent" -> /agents (dead link, no launcher)
                // Now: opens chat with a prefilled prompt asking the CEO to ship a blog post
                label: "Generate your first blog post",
                desc: "Ask the CEO to write a blog post -- it'll appear in your inbox",
                href: "/chat?prefill=write%20a%20blog%20post%20about%20%5Byour%20topic%5D",
                done: kpis.content_published.value > 0,
              },
              {
                // Was: "Set up email marketing" -> /agents
                // Now: opens chat with email-specific prefill
                label: "Draft your first marketing email",
                desc: "Ask the CEO to write a welcome email -- review it in your inbox",
                href: "/chat?prefill=draft%20a%20welcome%20email%20for%20new%20signups",
                done: kpis.emails_sent.value > 0,
              },
              {
                // Was: "Launch an ad campaign" -> /agents
                // Now: opens chat with ad strategy prefill
                label: "Plan your first ad campaign",
                desc: "Get a Facebook Ads setup guide tailored to your business",
                href: "/chat?prefill=give%20me%20a%20facebook%20ads%20strategy%20for%20my%20business",
                done: kpis.ad_spend.value > 0,
              },
            ].map((step) => (
              <a key={step.label} href={step.href} className="flex items-start gap-3 p-3 rounded-lg hover:bg-[#F8F8F6] transition-colors group">
                {step.done ? (
                  <div className="w-5 h-5 rounded-full bg-[#1D9E75] flex items-center justify-center mt-0.5 shrink-0">
                    <svg className="w-3 h-3 text-white" fill="none" stroke="currentColor" strokeWidth={3} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                    </svg>
                  </div>
                ) : (
                  <div className="w-5 h-5 rounded-full border-2 mt-0.5 shrink-0 border-[#E0DED8] group-hover:border-[#534AB7]" />
                )}
                <div>
                  <p className={`text-sm font-medium group-hover:text-[#534AB7] ${step.done ? "text-[#5F5E5A] line-through" : "text-[#2C2C2A]"}`}>{step.label}</p>
                  <p className="text-xs text-[#5F5E5A] mt-0.5">{step.desc}</p>
                </div>
              </a>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
