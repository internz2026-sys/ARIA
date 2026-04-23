"use client";

import React, { createContext, useContext, useEffect, useState, useCallback, useRef } from "react";
import { notificationsApi } from "@/lib/api";

export interface Notification {
  id: string;
  tenant_id: string;
  type: string;
  category: string;
  title: string;
  body: string;
  href: string;
  priority: string;
  is_read: boolean;
  is_seen: boolean;
  created_at: string;
  // Universal deep-link metadata — populated by the backend _notify
  // helper so every alert can route the user directly to the
  // specific asset (inbox row, CRM contact, project card, etc.)
  // without hardcoding per-type paths on the frontend.
  resource_type?: string | null;
  resource_id?: string | null;
  metadata?: Record<string, unknown> | null;
}

export interface BadgeCounts {
  inbox: number;
  conversations: number;
  system: number;
  total: number;
}

// Variants for client-triggered toasts (for action feedback like
// "Item deleted" / "Save failed"). Server-pushed notifications use
// `category` instead — these two systems share the same toast UI.
export type ToastVariant = "success" | "error" | "info" | "warning";

export interface ClientToastOptions {
  title: string;
  body?: string;
  variant?: ToastVariant;
  href?: string;
  durationMs?: number;
}

interface NotificationContextValue {
  notifications: Notification[];
  badges: BadgeCounts;
  toasts: Notification[];
  markAsRead: (ids?: string[]) => void;
  dismissToast: (id: string) => void;
  refetchCounts: () => void;
  /** Fire a client-side toast for action feedback (no server roundtrip). */
  showToast: (opts: ClientToastOptions) => void;
}

const NotificationContext = createContext<NotificationContextValue>({
  notifications: [],
  badges: { inbox: 0, conversations: 0, system: 0, total: 0 },
  toasts: [],
  markAsRead: () => {},
  dismissToast: () => {},
  refetchCounts: () => {},
  showToast: () => {},
});

export function useNotifications() {
  return useContext(NotificationContext);
}

const MAX_TOASTS = 3;
const TOAST_DURATION = 5000;

// Map notification category → sidebar route for badge matching
const CATEGORY_TO_ROUTE: Record<string, string> = {
  inbox: "/inbox",
  conversation: "/conversations",
  system: "/settings",
  status: "/inbox",
};

export function NotificationProvider({ children }: { children: React.ReactNode }) {
  const [notifications, setNotifications] = useState<Notification[]>([]);
  const [badges, setBadges] = useState<BadgeCounts>({ inbox: 0, conversations: 0, system: 0, total: 0 });
  const [toasts, setToasts] = useState<Notification[]>([]);
  const toastTimers = useRef<Map<string, NodeJS.Timeout>>(new Map());

  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchCounts = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await notificationsApi.counts(tenantId);
      setBadges({
        inbox: data.inbox_unread || 0,
        conversations: data.conversations_unread || 0,
        system: data.system_unread || 0,
        total: data.total_unread || 0,
      });
    } catch {}
  }, [tenantId]);

  const fetchNotifications = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await notificationsApi.list(tenantId, false, 30);
      setNotifications(data.notifications || []);
    } catch {}
  }, [tenantId]);

  // Initial load
  useEffect(() => {
    fetchCounts();
    fetchNotifications();
  }, [fetchCounts, fetchNotifications]);

  // Socket.IO real-time listener
  useEffect(() => {
    if (!tenantId) return;
    let socket: any = null;
    try {
      const { getSocket } = require("@/lib/socket");
      socket = getSocket();

      const handleNotification = (n: Notification) => {
        // Add to notifications list
        setNotifications(prev => [n, ...prev].slice(0, 50));

        // Optimistically bump only the categories that STILL reflect raw
        // notification-event counts. The `inbox` badge is now driven by
        // inbox_items action status (pending_approval / needs_review /
        // failed) — refetched when those items actually change, below.
        setBadges(prev => ({
          inbox: prev.inbox,
          conversations: prev.conversations + (n.category === "conversation" ? 1 : 0),
          system: prev.system + (n.category === "system" ? 1 : 0),
          total: prev.total + (n.category !== "inbox" ? 1 : 0),
        }));

        // Show toast for high-priority or important types
        if (n.priority === "high" || ["reply_received", "approval_needed", "system_alert", "gmail_disconnected"].includes(n.type)) {
          addToast(n);
          // Browser notification
          if (typeof window !== "undefined" && "Notification" in window && Notification.permission === "granted") {
            new Notification(n.title, {
              body: n.body || undefined,
              icon: "/favicon.ico",
              tag: n.id,
            });
          }
        }
      };

      // Any inbox_items mutation could change the action-needed count, so
      // refetch counts when one fires. Cheap — the backend query is a
      // single count(*) over an indexed column.
      const handleInboxMutation = () => { fetchCounts(); };

      // Multi-tab sync: when Tab A marks notifications read, the
      // backend emits `notifications_read` so Tab B can drop the same
      // rows' is_read flag locally without waiting for a manual refetch.
      // Payload: { ids: string[] } — empty array = mark-all-read.
      const handleNotificationsRead = (payload: { ids?: string[] } | null) => {
        const ids = payload?.ids || [];
        setNotifications(prev => prev.map(n => {
          if (ids.length === 0 || ids.includes(n.id)) {
            return { ...n, is_read: true };
          }
          return n;
        }));
        // Also refetch counts so the sidebar Inbox badge (driven by
        // inbox-action-needed, not notification is_read) stays correct.
        fetchCounts();
      };

      socket.on("notification", handleNotification);
      socket.on("notifications_read", handleNotificationsRead);
      socket.on("inbox_new_item", handleInboxMutation);
      socket.on("inbox_item_updated", handleInboxMutation);
      socket.on("inbox_item_deleted", handleInboxMutation);
      return () => {
        socket.off("notification", handleNotification);
        socket.off("notifications_read", handleNotificationsRead);
        socket.off("inbox_new_item", handleInboxMutation);
        socket.off("inbox_item_updated", handleInboxMutation);
        socket.off("inbox_item_deleted", handleInboxMutation);
      };
    } catch {}
  }, [tenantId, fetchCounts]);

  // Refetch counts on reconnect
  useEffect(() => {
    if (!tenantId) return;
    let socket: any = null;
    try {
      const { getSocket } = require("@/lib/socket");
      socket = getSocket();
      const handleReconnect = () => { fetchCounts(); fetchNotifications(); };
      socket.on("connect", handleReconnect);
      return () => { socket.off("connect", handleReconnect); };
    } catch {}
  }, [tenantId, fetchCounts, fetchNotifications]);

  function addToast(n: Notification) {
    setToasts(prev => {
      // Dedupe by id
      if (prev.some(t => t.id === n.id)) return prev;
      const next = [n, ...prev].slice(0, MAX_TOASTS);
      return next;
    });
    // Auto-dismiss
    const timer = setTimeout(() => dismissToast(n.id), TOAST_DURATION);
    toastTimers.current.set(n.id, timer);
  }

  // Client-triggered toast for action feedback ("Item deleted" / "Save failed").
  // Synthesizes a Notification-shaped object so it shares the same toast UI
  // as server-pushed notifications, but never hits the API. Variant maps to
  // category so the left-border color in ToastContainer reflects success/
  // error/info/warning.
  const showToast = useCallback((opts: ClientToastOptions) => {
    const variant: ToastVariant = opts.variant || "info";
    const variantToCategory: Record<ToastVariant, string> = {
      success: "conversation",  // green
      error: "system",          // orange
      info: "inbox",            // purple
      warning: "system",        // orange
    };
    const synthetic: Notification = {
      id: `client-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`,
      tenant_id: tenantId,
      type: `client_${variant}`,
      category: variantToCategory[variant],
      title: opts.title,
      body: opts.body || "",
      href: opts.href || "",
      priority: variant === "error" ? "high" : "medium",
      is_read: true,   // never goes in the bell badge
      is_seen: true,
      created_at: new Date().toISOString(),
    };
    setToasts(prev => {
      const next = [synthetic, ...prev].slice(0, MAX_TOASTS);
      return next;
    });
    const duration = opts.durationMs || (variant === "error" ? 7000 : TOAST_DURATION);
    const timer = setTimeout(() => dismissToast(synthetic.id), duration);
    toastTimers.current.set(synthetic.id, timer);
  }, [tenantId]);

  function dismissToast(id: string) {
    setToasts(prev => prev.filter(t => t.id !== id));
    const timer = toastTimers.current.get(id);
    if (timer) {
      clearTimeout(timer);
      toastTimers.current.delete(id);
    }
  }

  async function markAsRead(ids?: string[]) {
    if (!tenantId) return;

    // Optimistic update — apply the is_read=true change FIRST so the
    // bell counter drops to 0 immediately. The bell's badge reads
    // notifications.filter(!is_read).length (see NotificationBell.tsx)
    // so this alone zeros the UI before the API round-trip completes.
    const snapshot = notifications;
    setNotifications(prev => prev.map(n => {
      if (!ids || ids.length === 0) return { ...n, is_read: true };
      return ids.includes(n.id) ? { ...n, is_read: true } : n;
    }));

    try {
      await notificationsApi.markRead(tenantId, ids);
      // Re-sync the sidebar badges (conversations / system / inbox-
      // action-needed) with the authoritative backend counts.
      fetchCounts();
    } catch (err) {
      // Revert on failure so the user doesn't see a false "0" while
      // the server state is still unread. Surface a toast so the
      // failure isn't invisible.
      setNotifications(snapshot);
      showToast({
        title: "Couldn't mark as read",
        body: "Network error — try again in a moment.",
        variant: "error",
      });
    }
  }

  return (
    <NotificationContext.Provider value={{
      notifications,
      badges,
      toasts,
      markAsRead,
      dismissToast,
      refetchCounts: fetchCounts,
      showToast,
    }}>
      {children}
    </NotificationContext.Provider>
  );
}
