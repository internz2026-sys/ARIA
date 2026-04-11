"use client";

import React from "react";
import { useRouter } from "next/navigation";
import { useNotifications, Notification } from "@/lib/use-notifications";

const CATEGORY_COLORS: Record<string, string> = {
  inbox: "#534AB7",       // purple — info
  conversation: "#1D9E75", // green — success
  system: "#D85A30",       // orange — error/warning
  status: "#5F5E5A",       // grey — neutral
  email: "#2563eb",        // blue — email send confirmations
  social: "#1D9E75",       // green — social publish confirmations
};

export default function ToastContainer() {
  const { toasts, dismissToast } = useNotifications();
  const router = useRouter();

  if (toasts.length === 0) return null;

  function handleClick(t: Notification) {
    dismissToast(t.id);
    if (t.href) router.push(t.href);
  }

  return (
    <div className="fixed top-4 right-4 z-[70] flex flex-col gap-2 w-[360px] pointer-events-none">
      {toasts.map((t) => (
        <div
          key={t.id}
          onClick={() => handleClick(t)}
          className="pointer-events-auto bg-white rounded-xl shadow-lg border border-[#E0DED8] p-4 cursor-pointer hover:shadow-xl transition-shadow animate-slide-in-right"
          style={{ borderLeftWidth: 4, borderLeftColor: CATEGORY_COLORS[t.category] || "#534AB7" }}
        >
          <div className="flex items-start gap-3">
            <div className="flex-1 min-w-0">
              <p className="text-sm font-semibold text-[#2C2C2A] truncate">{t.title}</p>
              {t.body && (
                <p className="text-xs text-[#5F5E5A] mt-0.5 line-clamp-2">{t.body}</p>
              )}
            </div>
            <button
              onClick={(e) => { e.stopPropagation(); dismissToast(t.id); }}
              className="text-[#9E9C95] hover:text-[#2C2C2A] transition-colors shrink-0"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
