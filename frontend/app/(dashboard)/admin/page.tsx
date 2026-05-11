"use client";

import React, { useEffect, useState, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import { authFetch, API_URL, admin as adminApi } from "@/lib/api";
import { useConfirm } from "@/lib/use-confirm";
import { Panel } from "@/components/ui/panel";

// Role-based admin dashboard. Acts as its own guard: on mount, hits
// /api/admin/me — backend middleware short-circuits non-admins with
// 403, in which case we bounce the user back to /dashboard with a
// "Restricted access" toast. Avoids a Next.js middleware.ts because
// ARIA's session lives in localStorage (Supabase implicit flow) and
// can't be read server-side without migrating to cookie auth.

type Role = "user" | "admin" | "super_admin" | "banned";
type Status = "active" | "paused" | "suspended";

type AdminUser = {
  user_id: string;
  email: string | null;
  full_name: string | null;
  role: Role;
  status: Status;
  /** Non-null when the user is currently banned (ISO timestamp). */
  banned_at?: string | null;
  created_at: string;
  updated_at: string;
};

// ── Ban reason dialog ────────────────────────────────────────────────────────
// useConfirm doesn't support a textarea, so we use a small inline modal for
// the Ban flow. The Unban flow is simpler and reuses useConfirm as-is.

interface BanDialogProps {
  target: AdminUser;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}

function BanDialog({ target, onConfirm, onCancel }: BanDialogProps) {
  const [reason, setReason] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { textareaRef.current?.focus(); }, []);

  return (
    <div
      className="fixed inset-0 z-[80] flex items-center justify-center bg-black/40 backdrop-blur-sm"
      onClick={onCancel}
    >
      <div
        className="bg-white rounded-xl border border-[#E0DED8] shadow-2xl max-w-md w-full mx-4 overflow-hidden"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-6 pt-6 pb-2">
          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-red-50 flex items-center justify-center flex-shrink-0">
              <svg className="w-5 h-5 text-red-500" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
              </svg>
            </div>
            <h3 className="text-lg font-semibold text-[#2C2C2A]">Ban this user?</h3>
          </div>
        </div>
        <div className="px-6 py-4 space-y-3">
          <p className="text-sm text-[#5F5E5A] leading-relaxed">
            <strong>{target.email || target.user_id}</strong> will be unable to log in for 1 year (8,760 hours). You can lift the ban at any time using the Unban button.
          </p>
          <div>
            <label className="block text-xs font-medium text-[#5F5E5A] mb-1">
              Reason <span className="text-[#9E9C95] font-normal">(optional)</span>
            </label>
            <textarea
              ref={textareaRef}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              rows={3}
              placeholder="e.g. Abuse of service, repeated ToS violations..."
              className="w-full text-sm px-3 py-2 rounded-lg border border-[#E0DED8] focus:outline-none focus:ring-2 focus:ring-red-400/30 focus:border-red-400/60 resize-none"
            />
          </div>
        </div>
        <div className="px-6 pb-6 pt-2 flex items-center justify-end gap-3">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={() => onConfirm(reason)}
            className="px-4 py-2 text-sm font-medium rounded-lg text-white bg-red-500 hover:bg-red-600 transition-colors"
          >
            Ban user
          </button>
        </div>
      </div>
    </div>
  );
}

type Stats = {
  users_total: number;
  users_admin: number;
  tenants_total: number;
  agent_runs_total: number;
  inbox_items_total: number;
};

const ROLE_LABELS: Record<Role, string> = {
  user: "User",
  admin: "Admin",
  super_admin: "Super Admin",
  banned: "Banned",
};

const ROLE_BADGE: Record<Role, string> = {
  user: "bg-[#F0F0EE] text-[#5F5E5A] border-[#E0DED8]",
  admin: "bg-[#EEEDFE] text-[#534AB7] border-[#534AB7]/30",
  super_admin: "bg-[#FFF4D6] text-[#8A6D00] border-[#D4B24C]/40",
  banned: "bg-[#FDEEE8] text-[#B8491F] border-[#D85A30]/30",
};

// Badge used for the Banned status indicator in the Status column,
// separate from role badge so it works even when role !== "banned".
const BANNED_BADGE_CLS = "bg-[#FDEEE8] text-[#B8491F] border-[#D85A30]/30";

const STATUS_BADGE: Record<Status, string> = {
  active: "bg-[#E6F5ED] text-[#157A5A] border-[#1D9E75]/30",
  paused: "bg-[#FFF4D6] text-[#8A6D00] border-[#D4B24C]/40",
  suspended: "bg-[#FDEEE8] text-[#B8491F] border-[#D85A30]/30",
};

const STATUS_LABELS: Record<Status, string> = {
  active: "Active",
  paused: "Paused",
  suspended: "Suspended",
};

export default function AdminPage() {
  const router = useRouter();
  const [me, setMe] = useState<{ user_id: string; role: Role } | null>(null);
  const [authState, setAuthState] = useState<"checking" | "ok" | "denied">("checking");
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [stats, setStats] = useState<Stats | null>(null);
  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState<"" | Role>("");
  const [loading, setLoading] = useState(true);
  const [pendingChange, setPendingChange] = useState<string | null>(null);
  const [pendingAction, setPendingAction] = useState<{ user_id: string; kind: "reset" | "delete" | "ban" | "unban" } | null>(null);
  const [errorMsg, setErrorMsg] = useState<string>("");
  const [successMsg, setSuccessMsg] = useState<string>("");
  /** Non-null while the ban reason dialog is open for a specific user. */
  const [banTarget, setBanTarget] = useState<AdminUser | null>(null);
  const { confirm } = useConfirm();

  // Initial role check — single source of truth for whether this page
  // even renders. Backend returns 200 + {role} for admins, 403 for
  // everyone else.
  useEffect(() => {
    (async () => {
      try {
        const res = await authFetch(`${API_URL}/api/admin/me`);
        if (res.status === 403 || res.status === 401) {
          setAuthState("denied");
          setTimeout(() => router.replace("/dashboard"), 1600);
          return;
        }
        if (!res.ok) {
          setAuthState("denied");
          setTimeout(() => router.replace("/dashboard"), 1600);
          return;
        }
        const data = await res.json();
        setMe({ user_id: data.user_id, role: data.role });
        setAuthState("ok");
      } catch {
        setAuthState("denied");
        setTimeout(() => router.replace("/dashboard"), 1600);
      }
    })();
  }, [router]);

  const loadUsers = useCallback(async () => {
    if (authState !== "ok") return;
    setLoading(true);
    try {
      const params = new URLSearchParams();
      if (search) params.set("search", search);
      if (roleFilter) params.set("role", roleFilter);
      const res = await authFetch(`${API_URL}/api/admin/users?${params.toString()}`);
      if (res.ok) {
        const data = await res.json();
        setUsers(data.users || []);
      }
    } catch (e: any) {
      setErrorMsg(e?.message || "Failed to load users");
    }
    setLoading(false);
  }, [authState, search, roleFilter]);

  const loadStats = useCallback(async () => {
    if (authState !== "ok") return;
    try {
      const res = await authFetch(`${API_URL}/api/admin/stats`);
      if (res.ok) setStats(await res.json());
    } catch {
      // Stats are best-effort — silent fail keeps the table usable.
    }
  }, [authState]);

  useEffect(() => { loadUsers(); }, [loadUsers]);
  useEffect(() => { loadStats(); }, [loadStats]);

  const handleRoleChange = async (target: AdminUser, newRole: Role) => {
    if (newRole === target.role) return;
    setPendingChange(target.user_id);
    setErrorMsg("");
    try {
      const res = await authFetch(`${API_URL}/api/admin/users/${target.user_id}/role`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ role: newRole }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Failed (${res.status})`);
      }
      // Optimistic local update — server already accepted the change.
      setUsers((prev) => prev.map((u) =>
        u.user_id === target.user_id ? { ...u, role: newRole } : u,
      ));
      loadStats();
    } catch (e: any) {
      setErrorMsg(e?.message || "Couldn't change role");
    }
    setPendingChange(null);
  };

  const handleStatusChange = async (target: AdminUser, newStatus: Status) => {
    if (newStatus === target.status) return;
    // Confirm only when moving INTO a blocked status — restoring is one click.
    if (newStatus === "paused" || newStatus === "suspended") {
      const ok = await confirm({
        title: newStatus === "paused" ? "Pause this account?" : "Suspend this account?",
        message:
          `${target.email || target.user_id} will be blocked from sending messages to the CEO and running agents. They can still view their dashboard and inbox. ${newStatus === "suspended" ? "Suspended is harder than paused — reserved for abuse / billing holds." : "You can resume access at any time."}`,
        confirmLabel: newStatus === "paused" ? "Pause" : "Suspend",
        cancelLabel: "Cancel",
        destructive: newStatus === "suspended",
      });
      if (!ok) return;
    }
    setPendingChange(target.user_id);
    setErrorMsg("");
    setSuccessMsg("");
    try {
      const res = await authFetch(`${API_URL}/api/admin/users/${target.user_id}/status`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Failed (${res.status})`);
      }
      setUsers((prev) => prev.map((u) =>
        u.user_id === target.user_id ? { ...u, status: newStatus } : u,
      ));
      setSuccessMsg(
        newStatus === "active"
          ? `Resumed ${target.email || "user"}.`
          : `${newStatus === "paused" ? "Paused" : "Suspended"} ${target.email || "user"}.`,
      );
    } catch (e: any) {
      setErrorMsg(e?.message || "Couldn't change status");
    }
    setPendingChange(null);
  };

  const handleResetPassword = async (target: AdminUser) => {
    const ok = await confirm({
      title: "Send password reset link?",
      message: `${target.email} will receive an email with a link to set a new password. They'll need to click it within the next hour.`,
      confirmLabel: "Send reset link",
      cancelLabel: "Cancel",
    });
    if (!ok) return;
    setPendingAction({ user_id: target.user_id, kind: "reset" });
    setErrorMsg("");
    setSuccessMsg("");
    try {
      const res = await authFetch(`${API_URL}/api/admin/users/${target.user_id}/reset-password`, {
        method: "POST",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Failed (${res.status})`);
      setSuccessMsg(`Password reset link sent to ${body.email || target.email}.`);
    } catch (e: any) {
      setErrorMsg(e?.message || "Couldn't send reset link");
    }
    setPendingAction(null);
  };

  const handleDeleteUser = async (target: AdminUser) => {
    // Two-step confirm: a generic "are you sure" then a typed-email
    // gate would be the gold standard, but the existing confirm dialog
    // only supports one prompt. Use destructive styling + verbose
    // message so the user has to read what they're about to do.
    const ok = await confirm({
      title: "Delete this user permanently?",
      message:
        `${target.email || target.user_id} will be removed from auth, their profile cascaded out, and any onboarding drafts cleaned up. This CANNOT be undone. Tenant-scoped content (inbox items, agent runs) is preserved — clean it up separately if needed.`,
      confirmLabel: "Delete user",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    setPendingAction({ user_id: target.user_id, kind: "delete" });
    setErrorMsg("");
    setSuccessMsg("");
    try {
      const res = await authFetch(`${API_URL}/api/admin/users/${target.user_id}`, {
        method: "DELETE",
      });
      const body = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(body.detail || `Failed (${res.status})`);
      setUsers((prev) => prev.filter((u) => u.user_id !== target.user_id));
      setSuccessMsg(`Deleted ${body.email || target.email || "user"}.`);
      loadStats();
    } catch (e: any) {
      setErrorMsg(e?.message || "Couldn't delete user");
    }
    setPendingAction(null);
  };

  const handleBanUser = async (target: AdminUser, reason: string) => {
    setBanTarget(null);
    setPendingAction({ user_id: target.user_id, kind: "ban" });
    setErrorMsg("");
    setSuccessMsg("");
    try {
      const body = await adminApi.banUser(target.user_id, 8760, reason);
      // Reflect the ban locally — mark role as "banned" and store banned_at
      setUsers((prev) => prev.map((u) =>
        u.user_id === target.user_id
          ? { ...u, role: "banned" as Role, banned_at: body.banned_until ?? new Date().toISOString() }
          : u,
      ));
      setSuccessMsg(`Banned ${target.email || "user"} until ${body.banned_until ? new Date(body.banned_until).toLocaleDateString() : "1 year from now"}.`);
    } catch (e: any) {
      setErrorMsg(e?.message || "Couldn't ban user");
    }
    setPendingAction(null);
  };

  const handleUnbanUser = async (target: AdminUser) => {
    const ok = await confirm({
      title: "Unban this user?",
      message: `${target.email || target.user_id} will immediately regain the ability to log in.`,
      confirmLabel: "Unban",
      cancelLabel: "Cancel",
    });
    if (!ok) return;
    setPendingAction({ user_id: target.user_id, kind: "unban" });
    setErrorMsg("");
    setSuccessMsg("");
    try {
      await adminApi.unbanUser(target.user_id);
      setUsers((prev) => prev.map((u) =>
        u.user_id === target.user_id
          ? { ...u, role: "user" as Role, banned_at: null }
          : u,
      ));
      setSuccessMsg(`Unbanned ${target.email || "user"}.`);
    } catch (e: any) {
      setErrorMsg(e?.message || "Couldn't unban user");
    }
    setPendingAction(null);
  };

  if (authState === "checking") {
    return (
      <div className="min-h-[60vh] flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-[#5F5E5A]">Verifying access...</p>
        </div>
      </div>
    );
  }

  if (authState === "denied") {
    return (
      <div className="min-h-[60vh] flex items-center justify-center">
        <div className="max-w-md text-center bg-white rounded-2xl border border-[#E0DED8] p-8">
          <div className="w-12 h-12 rounded-full bg-[#FDEEE8] flex items-center justify-center mx-auto mb-3">
            <svg className="w-6 h-6 text-[#D85A30]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 15v2m-6.39-2.34a9 9 0 1112.78 0M5.62 4.38l12.76 12.76" />
            </svg>
          </div>
          <h2 className="text-lg font-semibold text-[#2C2C2A] mb-1">Restricted access</h2>
          <p className="text-sm text-[#5F5E5A]">
            You don't have permission to view the admin console. Returning you to the dashboard...
          </p>
        </div>
      </div>
    );
  }

  const isSuper = me?.role === "super_admin";

  /** True when a user row indicates a ban, via either signal the backend may provide. */
  const isBanned = (u: AdminUser) => Boolean(u.banned_at) || u.role === "banned";

  return (
    <>
    {/* Ban reason dialog — rendered outside the table so it can centre-overlay */}
    {banTarget && (
      <BanDialog
        target={banTarget}
        onConfirm={(reason) => handleBanUser(banTarget, reason)}
        onCancel={() => setBanTarget(null)}
      />
    )}
    <div className="max-w-screen-2xl mx-auto space-y-6">
      <div>
        <div className="flex items-center gap-3 mb-1">
          <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">Admin</h1>
          {me?.role && (
            <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${ROLE_BADGE[me.role]}`}>
              {ROLE_LABELS[me.role].toUpperCase()}
            </span>
          )}
        </div>
        <p className="text-sm text-[#5F5E5A]">User management + system overview</p>
      </div>

      {/* Stats cards */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {[
          { label: "Users", value: stats?.users_total ?? "—" },
          { label: "Admins", value: stats?.users_admin ?? "—" },
          { label: "Tenants", value: stats?.tenants_total ?? "—" },
          { label: "Agent Runs", value: stats?.agent_runs_total ?? "—" },
          { label: "Inbox Items", value: stats?.inbox_items_total ?? "—" },
        ].map((s) => (
          <Panel key={s.label} className="p-4">
            <p className="text-[10px] font-bold uppercase tracking-wide text-[#9E9C95]">{s.label}</p>
            <p className="text-xl font-bold text-[#2C2C2A] mt-1">{s.value}</p>
          </Panel>
        ))}
      </div>

      {/* Users table */}
      <Panel className="overflow-hidden">
        {/* Toolbar — on mobile the search+filter stack below the title row
            via flex-wrap so neither element blows the panel past 100vw. */}
        <div className="flex items-center gap-2 p-4 border-b border-[#E0DED8] flex-wrap">
          <h2 className="text-base font-semibold text-[#2C2C2A]">Users</h2>
          <span className="text-xs text-[#9E9C95]">{users.length} loaded</span>
          <div className="w-full sm:w-auto sm:ml-auto flex items-center gap-2 mt-2 sm:mt-0">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search email or name..."
              className="text-sm px-3 py-1.5 rounded-lg border border-[#E0DED8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7]/60 flex-1 sm:w-56 sm:flex-none min-w-0"
            />
            <select
              value={roleFilter}
              onChange={(e) => setRoleFilter(e.target.value as any)}
              className="text-sm px-2 py-1.5 rounded-lg border border-[#E0DED8] bg-white focus:outline-none flex-shrink-0"
            >
              <option value="">All roles</option>
              <option value="user">Users</option>
              <option value="admin">Admins</option>
              <option value="super_admin">Super Admins</option>
            </select>
          </div>
        </div>

        {errorMsg && (
          <div className="px-4 py-2 bg-[#FDEEE8] border-b border-[#D85A30]/20 text-sm text-[#B8491F]">
            {errorMsg}
          </div>
        )}
        {successMsg && (
          <div className="px-4 py-2 bg-[#E6F5ED] border-b border-[#1D9E75]/20 text-sm text-[#157A5A]">
            {successMsg}
          </div>
        )}

        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="text-left text-xs uppercase text-[#9E9C95] border-b border-[#E0DED8]">
                <th className="px-4 py-2 font-semibold">Email</th>
                <th className="px-4 py-2 font-semibold">Name</th>
                <th className="px-4 py-2 font-semibold">Role</th>
                <th className="px-4 py-2 font-semibold">Status</th>
                <th className="px-4 py-2 font-semibold">Joined</th>
                <th className="px-4 py-2 font-semibold">Last change</th>
                <th className="px-4 py-2 font-semibold">Set role</th>
                <th className="px-4 py-2 font-semibold text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && users.length === 0 ? (
                <tr><td colSpan={8} className="px-4 py-8 text-center text-[#9E9C95]">Loading...</td></tr>
              ) : users.length === 0 ? (
                <tr><td colSpan={8} className="px-4 py-8 text-center text-[#9E9C95]">No users match.</td></tr>
              ) : users.map((u) => {
                const isSelf = me?.user_id === u.user_id;
                const isTargetSuper = u.role === "super_admin";
                // Admins can't touch super_admin rows; super_admins can change anyone but
                // themselves. The server enforces this too — UI is just the friendly hint.
                const canEdit = !isSelf && (isSuper || !isTargetSuper);
                return (
                  <tr key={u.user_id} className="border-b border-[#F0EFEC] hover:bg-[#FAFAFA]">
                    <td className="px-4 py-3 text-[#2C2C2A]">
                      {u.email || <span className="text-[#9E9C95] italic">no email</span>}
                      {isSelf && <span className="ml-2 text-[10px] font-medium text-[#534AB7]">you</span>}
                    </td>
                    <td className="px-4 py-3 text-[#5F5E5A]">{u.full_name || "—"}</td>
                    <td className="px-4 py-3">
                      <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${ROLE_BADGE[u.role]}`}>
                        {ROLE_LABELS[u.role].toUpperCase()}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      <div className="flex flex-wrap items-center gap-1">
                        <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${STATUS_BADGE[u.status || "active"]}`}>
                          {STATUS_LABELS[u.status || "active"].toUpperCase()}
                        </span>
                        {isBanned(u) && (
                          <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full border ${BANNED_BADGE_CLS}`}
                            title={u.banned_at ? `Banned until ${new Date(u.banned_at).toLocaleDateString()}` : "Banned"}>
                            {u.banned_at ? `BANNED UNTIL ${new Date(u.banned_at).toLocaleDateString()}` : "BANNED"}
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-4 py-3 text-[#9E9C95] text-xs">
                      {u.created_at ? new Date(u.created_at).toLocaleDateString() : "—"}
                    </td>
                    <td className="px-4 py-3 text-[#9E9C95] text-xs">
                      {u.updated_at ? new Date(u.updated_at).toLocaleDateString() : "—"}
                    </td>
                    <td className="px-4 py-3">
                      <select
                        value={u.role}
                        disabled={!canEdit || pendingChange === u.user_id}
                        onChange={(e) => handleRoleChange(u, e.target.value as Role)}
                        className="text-sm px-2 py-1 rounded-lg border border-[#E0DED8] bg-white focus:outline-none disabled:opacity-50 disabled:cursor-not-allowed"
                        title={!canEdit ? (isSelf ? "Can't change your own role" : "Only a super admin can change a super admin") : ""}
                      >
                        <option value="user">User</option>
                        <option value="admin">Admin</option>
                        {/* super_admin only selectable when the actor is a super_admin */}
                        {isSuper && <option value="super_admin">Super Admin</option>}
                      </select>
                    </td>
                    <td className="px-4 py-3 text-right">
                      {/* Actions: reset password + delete. Hidden on
                          self (use the public flow instead) and on
                          targets the actor can't manage (admins can't
                          touch other admins / super_admins).
                          On mobile (< lg) we show icon-only buttons to
                          keep the actions column narrow enough to fit
                          the 760px min-width table inside overflow-x-auto.
                          Text labels appear at lg+ where there is more room. */}
                      <div className="inline-flex items-center gap-1">
                        {/* Pause/Resume */}
                        {(u.status || "active") === "active" ? (
                          <button
                            onClick={() => handleStatusChange(u, "paused")}
                            disabled={!canEdit || pendingChange === u.user_id}
                            className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-[#D4B24C]/40 text-[#8A6D00] bg-white hover:bg-[#FFF4D6] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                            title={!canEdit ? (isSelf ? "Can't pause your own account" : "Only a super admin can pause another admin") : "Block agent runs for this user"}
                          >
                            {/* Pause icon */}
                            <svg className="w-3.5 h-3.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                              <path d="M5.75 3a.75.75 0 00-.75.75v12.5c0 .414.336.75.75.75h1.5a.75.75 0 00.75-.75V3.75A.75.75 0 007.25 3h-1.5zm7 0a.75.75 0 00-.75.75v12.5c0 .414.336.75.75.75h1.5a.75.75 0 00.75-.75V3.75A.75.75 0 0014.25 3h-1.5z" />
                            </svg>
                            <span className="hidden lg:inline">
                              {pendingChange === u.user_id ? "Pausing…" : "Pause"}
                            </span>
                          </button>
                        ) : (
                          <button
                            onClick={() => handleStatusChange(u, "active")}
                            disabled={!canEdit || pendingChange === u.user_id}
                            className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-[#1D9E75]/30 text-[#157A5A] bg-white hover:bg-[#E6F5ED] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                            title={!canEdit ? (isSelf ? "Can't change your own status" : "Only a super admin can resume another admin") : "Restore agent access for this user"}
                          >
                            {/* Play/resume icon */}
                            <svg className="w-3.5 h-3.5 flex-shrink-0" fill="currentColor" viewBox="0 0 20 20">
                              <path d="M6.3 2.841A1.5 1.5 0 004 4.11v11.78a1.5 1.5 0 002.3 1.269l9.344-5.89a1.5 1.5 0 000-2.538L6.3 2.84z" />
                            </svg>
                            <span className="hidden lg:inline">
                              {pendingChange === u.user_id ? "Resuming…" : "Resume"}
                            </span>
                          </button>
                        )}
                        {/* Reset password */}
                        <button
                          onClick={() => handleResetPassword(u)}
                          disabled={!canEdit || pendingAction?.user_id === u.user_id}
                          className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-[#E0DED8] text-[#5F5E5A] bg-white hover:border-[#534AB7]/40 hover:text-[#534AB7] hover:bg-[#FAFAFF] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={!canEdit ? (isSelf ? "Use the public forgot-password flow for self-reset" : "Only a super admin can reset another admin") : "Send password reset email"}
                        >
                          {/* Key icon */}
                          <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 5.25a3 3 0 013 3m3 0a6 6 0 01-7.029 5.912c-.563-.097-1.159.026-1.563.43L10.5 17.25H8.25v2.25H6v2.25H2.25v-2.818c0-.597.237-1.17.659-1.591l6.499-6.499c.404-.404.527-1 .43-1.563A6 6 0 1121.75 8.25z" />
                          </svg>
                          <span className="hidden lg:inline">
                            {pendingAction?.user_id === u.user_id && pendingAction.kind === "reset" ? "Sending…" : "Reset pw"}
                          </span>
                        </button>
                        {/* Ban / Unban — destructive; only super_admins can ban */}
                        {canEdit && isSuper && (
                          isBanned(u) ? (
                            <button
                              onClick={() => handleUnbanUser(u)}
                              disabled={pendingAction?.user_id === u.user_id}
                              className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-[#FBC9B9] text-[#D85A30] bg-white hover:bg-[#FDEEE8] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                              title="Lift the ban — user will be able to log in again"
                            >
                              {/* Check/unban icon */}
                              <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M13.5 10.5V6.75a4.5 4.5 0 119 0v3.75M3.75 21.75h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H3.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
                              </svg>
                              <span className="hidden lg:inline">
                                {pendingAction?.user_id === u.user_id && pendingAction.kind === "unban" ? "Unbanning…" : "Unban"}
                              </span>
                            </button>
                          ) : (
                            <button
                              onClick={() => setBanTarget(u)}
                              disabled={pendingAction?.user_id === u.user_id}
                              className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-[#FBC9B9] text-[#D85A30] bg-white hover:bg-[#FDEEE8] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                              title="Ban this user — blocks login for 1 year"
                            >
                              {/* Ban/lock icon */}
                              <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" d="M16.5 10.5V6.75a4.5 4.5 0 10-9 0v3.75m-.75 11.25h10.5a2.25 2.25 0 002.25-2.25v-6.75a2.25 2.25 0 00-2.25-2.25H6.75a2.25 2.25 0 00-2.25 2.25v6.75a2.25 2.25 0 002.25 2.25z" />
                              </svg>
                              <span className="hidden lg:inline">
                                {pendingAction?.user_id === u.user_id && pendingAction.kind === "ban" ? "Banning…" : "Ban"}
                              </span>
                            </button>
                          )
                        )}
                        {/* Delete */}
                        <button
                          onClick={() => handleDeleteUser(u)}
                          disabled={!canEdit || pendingAction?.user_id === u.user_id}
                          className="flex items-center gap-1 text-xs px-2 py-1 rounded-md border border-[#FBC9B9] text-[#D85A30] bg-white hover:bg-[#FDEEE8] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={!canEdit ? (isSelf ? "Can't delete your own account" : "Only a super admin can delete another admin") : "Permanently delete this user"}
                        >
                          {/* Trash icon */}
                          <svg className="w-3.5 h-3.5 flex-shrink-0" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                          </svg>
                          <span className="hidden lg:inline">
                            {pendingAction?.user_id === u.user_id && pendingAction.kind === "delete" ? "Deleting…" : "Delete"}
                          </span>
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
    </>
  );
}
