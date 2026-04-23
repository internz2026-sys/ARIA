"use client";

import React, { useState, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useNotifications, Notification } from "@/lib/use-notifications";
import { formatDateAgo, cleanNotificationBody, stripMarkdown } from "@/lib/utils";

const CATEGORY_COLORS: Record<string, { bg: string; text: string; label: string }> = {
  inbox: { bg: "bg-[#EEEDFE]", text: "text-[#534AB7]", label: "Inbox" },
  conversation: { bg: "bg-emerald-50", text: "text-emerald-700", label: "Conversation" },
  system: { bg: "bg-red-50", text: "text-red-600", label: "System" },
  status: { bg: "bg-amber-50", text: "text-amber-700", label: "Status" },
};


export default function NotificationBell() {
  const { notifications, markAsRead } = useNotifications();
  const [open, setOpen] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);
  const router = useRouter();

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  function handleNotificationClick(n: Notification) {
    // Always mark-as-read + close the dropdown, even when we can't
    // route anywhere. The user clicked it, they've seen it.
    if (!n.is_read) markAsRead([n.id]);
    setOpen(false);

    // Deep-link target resolution. Priority:
    //   1. Explicit n.href from backend (e.g. /inbox?id=<uuid> for
    //      newly-landed drafts, /calendar for scheduled tasks,
    //      /conversations for email replies)
    //   2. Category fallback for older notifications without an href
    //      or for ones where the backend only stored a category
    let target = (n.href || "").trim();
    if (!target) {
      switch (n.category) {
        case "inbox": target = "/inbox"; break;
        case "conversation": target = "/conversations"; break;
        case "status": target = "/calendar"; break;
        case "system": target = "/settings"; break;
        default: target = "/dashboard";
      }
    }
    // Only navigate to in-app paths. Avoid any absolute URL leaking
    // in from a prompt-injection-ish payload or an old migration.
    if (!target.startsWith("/")) return;
    router.push(target);
  }

  // Count is derived from the local notifications array (the same
  // array the dropdown renders below), NOT from the backend's
  // total_unread badge counter. That number rolls up inbox-action-needed
  // items AND notification rows, so "Mark all read" on the bell never
  // zeroed it out — the action-needed portion wasn't a notification in
  // the first place. Counting local is_read=false rows guarantees the
  // badge matches exactly what the user sees in the panel, and
  // markAsRead's optimistic update drops the count to 0 instantly.
  const count = notifications.reduce((n, x) => n + (x.is_read ? 0 : 1), 0);
  const displayCount = count > 99 ? "99+" : count;

  return (
    <div className="relative" ref={panelRef}>
      <button
        onClick={() => setOpen(!open)}
        className="relative p-2.5 rounded-xl text-[#5F5E5A] hover:bg-[#F8F8F6] hover:text-[#2C2C2A] transition-colors"
        title="Notifications"
      >
        <svg className="w-7 h-7" fill="none" stroke="currentColor" strokeWidth={1.8} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
        </svg>
        {count > 0 && (
          <span className="absolute -top-0.5 -right-0.5 min-w-[20px] h-[20px] flex items-center justify-center bg-[#D85A30] text-white text-[11px] font-bold rounded-full px-1.5">
            {displayCount}
          </span>
        )}
      </button>

      {open && (
        // Mobile: shrink to viewport width minus 1rem of padding so the
        // dropdown never overflows the right edge. Desktop: the original
        // 380px fixed width. `right-0` anchors the right edge so it
        // grows leftward on small screens rather than spilling off.
        <div className="absolute right-0 top-full mt-2 w-[calc(100vw-2rem)] max-w-[380px] bg-white rounded-xl border border-[#E0DED8] shadow-xl z-[80] overflow-hidden">
          {/* Header — "Mark all read" always visible when there's any
              notification at all, disabled when nothing's unread so the
              user can see the affordance even after clearing. */}
          <div className="flex items-center justify-between px-4 py-3 border-b border-[#E0DED8]">
            <h3 className="text-sm font-semibold text-[#2C2C2A]">Notifications</h3>
            {notifications.length > 0 && (
              <button
                onClick={() => markAsRead()}
                disabled={count === 0}
                className="text-xs font-medium text-[#534AB7] hover:text-[#4840A0] disabled:text-[#B0AFA8] disabled:cursor-not-allowed disabled:hover:text-[#B0AFA8]"
                title={count === 0 ? "All notifications are already read" : "Mark all as read"}
              >
                Mark all read
              </button>
            )}
          </div>

          {/* List */}
          <div className="max-h-[400px] overflow-y-auto">
            {notifications.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-[#9E9C95]">
                No notifications yet
              </div>
            ) : (
              notifications.slice(0, 30).map((n) => {
                const cat = CATEGORY_COLORS[n.category] || CATEGORY_COLORS.status;
                return (
                  // Row is a flex container, NOT a <button>. The unread
                  // dot is its own button (mark-as-read only, no nav)
                  // and the text area is a separate button (navigate +
                  // mark-as-read). Clicking the dot doesn't hide the
                  // notification — it just turns off the dot so the
                  // user can keep browsing without routing away.
                  <div
                    key={n.id}
                    className={`px-2 py-3 border-b border-[#F0EFEC] flex items-start gap-2 ${
                      !n.is_read ? "bg-[#FAFAFF]" : ""
                    }`}
                  >
                    {/* Unread-dot button: mark-as-read, do NOT navigate */}
                    <button
                      onClick={(e) => {
                        e.stopPropagation();
                        if (!n.is_read) markAsRead([n.id]);
                      }}
                      title={n.is_read ? "Already read" : "Mark as read"}
                      className="pt-1.5 px-1 shrink-0 group"
                      aria-label={n.is_read ? "Already read" : "Mark as read"}
                    >
                      {!n.is_read ? (
                        <div className="w-2 h-2 rounded-full bg-[#534AB7] group-hover:bg-[#3B3386] transition-colors" />
                      ) : (
                        <div className="w-2 h-2 rounded-full border border-[#E0DED8] group-hover:border-[#C5C3BC] transition-colors" />
                      )}
                    </button>
                    {/* Content button: navigate + mark-as-read */}
                    <button
                      onClick={() => handleNotificationClick(n)}
                      className="flex-1 min-w-0 text-left px-2 -mx-1 py-0 rounded hover:bg-[#F8F8F6] transition-colors"
                    >
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-[#2C2C2A] truncate flex-1">
                          {stripMarkdown(n.title)}
                        </span>
                        <span className="text-[10px] text-[#9E9C95] shrink-0">
                          {formatDateAgo(n.created_at)}
                        </span>
                      </div>
                      {n.body && (
                        <p className="text-xs text-[#5F5E5A] mt-0.5 truncate">{cleanNotificationBody(n.body)}</p>
                      )}
                      <span className={`inline-block text-[10px] px-1.5 py-0.5 rounded-full mt-1 ${cat.bg} ${cat.text}`}>
                        {cat.label}
                      </span>
                    </button>
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
