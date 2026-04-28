"use client";

import React, { useEffect, useState, useCallback } from "react";
import { useRouter } from "next/navigation";
import { authFetch, API_URL } from "@/lib/api";
import { useConfirm } from "@/lib/use-confirm";

// Role-based admin dashboard. Acts as its own guard: on mount, hits
// /api/admin/me — backend middleware short-circuits non-admins with
// 403, in which case we bounce the user back to /dashboard with a
// "Restricted access" toast. Avoids a Next.js middleware.ts because
// ARIA's session lives in localStorage (Supabase implicit flow) and
// can't be read server-side without migrating to cookie auth.

type Role = "user" | "admin" | "super_admin";

type AdminUser = {
  user_id: string;
  email: string | null;
  full_name: string | null;
  role: Role;
  created_at: string;
  updated_at: string;
};

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
};

const ROLE_BADGE: Record<Role, string> = {
  user: "bg-[#F0F0EE] text-[#5F5E5A] border-[#E0DED8]",
  admin: "bg-[#EEEDFE] text-[#534AB7] border-[#534AB7]/30",
  super_admin: "bg-[#FFF4D6] text-[#8A6D00] border-[#D4B24C]/40",
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
  const [pendingAction, setPendingAction] = useState<{ user_id: string; kind: "reset" | "delete" } | null>(null);
  const [errorMsg, setErrorMsg] = useState<string>("");
  const [successMsg, setSuccessMsg] = useState<string>("");
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

  return (
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
          <div key={s.label} className="bg-white rounded-xl border border-[#E0DED8] p-4">
            <p className="text-[10px] font-bold uppercase tracking-wide text-[#9E9C95]">{s.label}</p>
            <p className="text-xl font-bold text-[#2C2C2A] mt-1">{s.value}</p>
          </div>
        ))}
      </div>

      {/* Users table */}
      <div className="bg-white rounded-xl border border-[#E0DED8]">
        <div className="flex items-center gap-2 p-4 border-b border-[#E0DED8] flex-wrap">
          <h2 className="text-base font-semibold text-[#2C2C2A]">Users</h2>
          <span className="text-xs text-[#9E9C95]">{users.length} loaded</span>
          <div className="ml-auto flex items-center gap-2">
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search email or name..."
              className="text-sm px-3 py-1.5 rounded-lg border border-[#E0DED8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7]/60 w-56"
            />
            <select
              value={roleFilter}
              onChange={(e) => setRoleFilter(e.target.value as any)}
              className="text-sm px-2 py-1.5 rounded-lg border border-[#E0DED8] bg-white focus:outline-none"
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
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-xs uppercase text-[#9E9C95] border-b border-[#E0DED8]">
                <th className="px-4 py-2 font-semibold">Email</th>
                <th className="px-4 py-2 font-semibold">Name</th>
                <th className="px-4 py-2 font-semibold">Role</th>
                <th className="px-4 py-2 font-semibold">Joined</th>
                <th className="px-4 py-2 font-semibold">Last change</th>
                <th className="px-4 py-2 font-semibold">Set role</th>
                <th className="px-4 py-2 font-semibold text-right">Actions</th>
              </tr>
            </thead>
            <tbody>
              {loading && users.length === 0 ? (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-[#9E9C95]">Loading...</td></tr>
              ) : users.length === 0 ? (
                <tr><td colSpan={7} className="px-4 py-8 text-center text-[#9E9C95]">No users match.</td></tr>
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
                          touch other admins / super_admins). */}
                      <div className="inline-flex items-center gap-1.5">
                        <button
                          onClick={() => handleResetPassword(u)}
                          disabled={!canEdit || pendingAction?.user_id === u.user_id}
                          className="text-xs px-2 py-1 rounded-md border border-[#E0DED8] text-[#5F5E5A] bg-white hover:border-[#534AB7]/40 hover:text-[#534AB7] hover:bg-[#FAFAFF] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={!canEdit ? (isSelf ? "Use the public forgot-password flow for self-reset" : "Only a super admin can reset another admin") : "Send password reset email"}
                        >
                          {pendingAction?.user_id === u.user_id && pendingAction.kind === "reset" ? "Sending..." : "Reset password"}
                        </button>
                        <button
                          onClick={() => handleDeleteUser(u)}
                          disabled={!canEdit || pendingAction?.user_id === u.user_id}
                          className="text-xs px-2 py-1 rounded-md border border-[#FBC9B9] text-[#D85A30] bg-white hover:bg-[#FDEEE8] transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                          title={!canEdit ? (isSelf ? "Can't delete your own account" : "Only a super admin can delete another admin") : "Permanently delete this user"}
                        >
                          {pendingAction?.user_id === u.user_id && pendingAction.kind === "delete" ? "Deleting..." : "Delete"}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
