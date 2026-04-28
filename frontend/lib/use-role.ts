"use client";

import { useEffect, useState } from "react";
import { API_URL, authFetch } from "@/lib/api";

/**
 * Fetches the current user's RBAC role from `/api/admin/me`. Backend
 * returns `200 + { role }` for admins, `401/403/404` for everyone else.
 *
 * Cached at the module level for the lifetime of the page so the
 * sidebar, dashboard, and admin page don't all fire their own request
 * on mount. Role changes are rare; if a super_admin promotes someone
 * mid-session, that user will pick up the new role on next navigation
 * (or after a hard refresh) — the 60s backend cache + this in-memory
 * cache stack to ~hard-refresh-required, which matches how Supabase
 * sessions work anyway.
 */
export type Role = "user" | "admin" | "super_admin";

let cached: Role | null = null;
let pending: Promise<Role> | null = null;

async function fetchRole(): Promise<Role> {
  if (cached) return cached;
  if (pending) return pending;
  pending = (async () => {
    try {
      const res = await authFetch(`${API_URL}/api/admin/me`);
      if (!res.ok) {
        cached = "user";
        return cached;
      }
      const data = await res.json();
      const role = (data?.role as Role) || "user";
      cached = role;
      return role;
    } catch {
      cached = "user";
      return cached;
    } finally {
      pending = null;
    }
  })();
  return pending;
}

/** Drop the cached role — call after a sign-in/sign-out so a fresh
 *  session re-resolves its role from scratch. */
export function clearRoleCache() {
  cached = null;
  pending = null;
}

export interface UseRole {
  role: Role;
  isAdmin: boolean;
  isSuperAdmin: boolean;
  loading: boolean;
}

/** React hook — returns `loading=true` until the first fetch resolves,
 *  then `role` + derived booleans. Returns `user` (and `isAdmin=false`)
 *  on any failure so callers can safely render gated UI without
 *  worrying about a transient flash of admin content. */
export function useRole(): UseRole {
  const [role, setRole] = useState<Role>(cached ?? "user");
  const [loading, setLoading] = useState(cached === null);

  useEffect(() => {
    let cancelled = false;
    fetchRole().then((r) => {
      if (cancelled) return;
      setRole(r);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  return {
    role,
    isAdmin: role === "admin" || role === "super_admin",
    isSuperAdmin: role === "super_admin",
    loading,
  };
}
