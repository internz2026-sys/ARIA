"use client";

import React, { useState, useRef, useEffect } from "react";
import { useRouter } from "next/navigation";
import { useNotifications, Notification } from "@/lib/use-notifications";
import { formatDateAgo, cleanNotificationBody, stripMarkdown } from "@/lib/utils";
import { getRouteForItem } from "@/lib/notification-routing";
import { setNotificationsOpen } from "@/lib/use-ui-overlay";

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

  // Broadcast open state to the overlay store so the CEO Chat FAB
  // hides while the panel is up — they used to stack in the
  // bottom-right corner. Cleared on unmount in case the bell
  // unmounts while open (route change with the panel still up).
  useEffect(() => {
    setNotificationsOpen(open);
    return () => {
      setNotificationsOpen(false);
    };
  }, [open]);

  // Close on Escape — the panel now has an explicit backdrop click
  // handler for outside-click, so the old document-level mousedown
  // listener is unnecessary and was double-firing on backdrop taps.
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  function handleNotificationClick(n: Notification) {
    // Always mark-as-read + close the dropdown, even when we can't
    // route anywhere. The user clicked it, they've seen it.
    if (!n.is_read) markAsRead([n.id]);
    setOpen(false);

    // Delegate to the shared routing utility so every deep-link
    // entry point (bell, email, in-app alerts) uses the same
    // category / resource_type → path table.
    const target = getRouteForItem(n as any);
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
    <>
      <button
        onClick={() => setOpen(!open)}
        className="relative p-2.5 rounded-xl text-[#5F5E5A] hover:bg-[#F8F8F6] hover:text-[#2C2C2A] transition-colors"
        title="Notifications"
        aria-expanded={open}
        aria-controls="notifications-panel"
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
        <>
          {/* Dim/blur backdrop — clicking it closes the panel. Sits
              just below the panel in z-order so the panel itself
              receives clicks without the backdrop swallowing them.
              The original `absolute` dropdown lived inside the
              header's stacking context and was being painted under
              the CEO Chat FAB; switching to a `fixed` portal-like
              layer at z-[90] (above FAB's z-[60]) puts notifications
              in their own stacking context above all dashboard noise. */}
          <div
            onClick={() => setOpen(false)}
            className="fixed inset-0 z-[85] bg-black/30 backdrop-blur-sm"
            aria-hidden="true"
          />

          {/* Panel — anchored to the right edge with a fixed height so
              long notification lists scroll inside the panel rather
              than pushing it off screen. Width: full-bleed-minus-pad
              on phones, 380px on tablets+, 420px on lg+. */}
          <div
            id="notifications-panel"
            ref={panelRef}
            role="dialog"
            aria-label="Notifications"
            className="fixed right-2 sm:right-4 top-[72px] z-[90] flex flex-col w-[calc(100vw-1rem)] sm:w-[380px] lg:w-[420px] h-[calc(100vh-100px)] bg-white rounded-xl border border-[#E0DED8] shadow-2xl overflow-hidden"
          >
            {/* Sticky header — stays visible while the user scrolls a
                long list. "Mark all read" is always rendered when
                there's any notification at all, disabled when nothing
                is unread so the affordance stays discoverable. The
                explicit Close (X) button is the primary mobile escape
                hatch since the backdrop and Escape may not be
                obvious to a phone user. */}
            <div className="flex items-center justify-between px-4 py-3 border-b border-[#E0DED8] bg-white shrink-0">
              <h3 className="text-sm font-semibold text-[#2C2C2A]">Notifications</h3>
              <div className="flex items-center gap-3">
                {notifications.length > 0 && (
                  <button
                    onClick={() => markAsRead()}
                    disabled={count === 0}
                    className="text-xs font-medium text-[#534AB7] hover:text-[#433AA0] disabled:text-[#B0AFA8] disabled:cursor-not-allowed disabled:hover:text-[#B0AFA8]"
                    title={count === 0 ? "All notifications are already read" : "Mark all as read"}
                  >
                    Mark all read
                  </button>
                )}
                <button
                  onClick={() => setOpen(false)}
                  className="p-1 -mr-1 rounded hover:bg-[#F8F8F6] text-[#5F5E5A] hover:text-[#2C2C2A] transition-colors"
                  title="Close notifications"
                  aria-label="Close notifications"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            </div>

            {/* Scrollable list — flex-1 so it consumes the remaining
                height under the sticky header, with overflow-y-auto so
                the panel itself never grows past the viewport. */}
            <div className="flex-1 overflow-y-auto">
              {notifications.length === 0 ? (
                <div className="px-4 py-8 text-center text-sm text-[#9E9C95]">
                  No notifications yet
                </div>
              ) : (
                notifications.slice(0, 30).map((n) => {
                  const cat = CATEGORY_COLORS[n.category] || CATEGORY_COLORS.status;
                  return (
                    <div
                      key={n.id}
                      className={`px-2 py-3 border-b border-[#F0EFEC] flex items-start gap-2 ${
                        !n.is_read ? "bg-[#FAFAFF]" : ""
                      }`}
                    >
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
        </>
      )}
    </>
  );
}
