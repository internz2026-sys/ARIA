"use client";

import React from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useNotifications } from "@/lib/use-notifications";

/**
 * Mobile bottom-tab bar — shown only on `<lg` viewports (hidden on
 * desktop where the left sidebar takes over). Covers the FOUR primary
 * routes; the hamburger drawer still handles everything else so no
 * navigation is lost. iPhone home-indicator safe-area handled via
 * `env(safe-area-inset-bottom)` so the tabs don't overlap the
 * OS gesture area.
 */

type TabDef = {
  label: string;
  href: string;
  match: (pathname: string) => boolean;
  badgeKey?: "inbox" | "conversations";
  icon: React.ReactNode;
};

const TABS: TabDef[] = [
  {
    label: "Home",
    href: "/dashboard",
    match: (p) => p === "/dashboard" || p === "/",
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0a1 1 0 01-1-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 01-1 1h-2z" />
      </svg>
    ),
  },
  {
    label: "Chat",
    href: "/chat",
    match: (p) => p.startsWith("/chat"),
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M7.5 8.25h9m-9 3H12m-9.75 1.51c0 1.6 1.123 2.994 2.707 3.227 1.129.166 2.27.293 3.423.379.35.026.67.21.865.501L12 21l2.755-4.133a1.14 1.14 0 01.865-.501 48.172 48.172 0 003.423-.379c1.584-.233 2.707-1.626 2.707-3.228V6.741c0-1.602-1.123-2.995-2.707-3.228A48.394 48.394 0 0012 3c-2.392 0-4.744.175-7.043.513C3.373 3.746 2.25 5.14 2.25 6.741v6.018z" />
      </svg>
    ),
  },
  {
    label: "Inbox",
    href: "/inbox",
    match: (p) => p.startsWith("/inbox"),
    badgeKey: "inbox",
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M20 13V6a2 2 0 00-2-2H6a2 2 0 00-2 2v7m16 0v5a2 2 0 01-2 2H6a2 2 0 01-2-2v-5m16 0h-2.586a1 1 0 00-.707.293l-2.414 2.414a1 1 0 01-.707.293h-3.172a1 1 0 01-.707-.293l-2.414-2.414A1 1 0 006.586 13H4" />
      </svg>
    ),
  },
  {
    label: "Chats",
    href: "/conversations",
    match: (p) => p.startsWith("/conversations"),
    badgeKey: "conversations",
    icon: (
      <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
      </svg>
    ),
  },
];

export default function MobileBottomNav() {
  const pathname = usePathname() || "";
  const { badges } = useNotifications();

  return (
    <nav
      className="lg:hidden fixed bottom-0 inset-x-0 z-40 bg-white border-t border-[#E0DED8] flex"
      // Respect iOS home-indicator safe-area so the tap targets don't
      // collide with the bottom gesture zone.
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
      aria-label="Primary"
    >
      {TABS.map((t) => {
        const active = t.match(pathname);
        const badgeCount =
          t.badgeKey === "inbox" ? badges.inbox :
          t.badgeKey === "conversations" ? badges.conversations :
          0;
        return (
          <Link
            key={t.href}
            href={t.href}
            // 44x44+ tap target by virtue of the 56px nav bar height.
            // flex-1 gives each tab equal viewport share.
            className={`flex-1 flex flex-col items-center justify-center py-2 gap-0.5 transition-colors ${
              active
                ? "text-[#534AB7]"
                : "text-[#5F5E5A] hover:text-[#2C2C2A]"
            }`}
            aria-current={active ? "page" : undefined}
          >
            <div className="relative">
              {t.icon}
              {badgeCount > 0 && (
                <span className="absolute -top-1.5 -right-2 min-w-[16px] h-[16px] px-1 flex items-center justify-center bg-[#D85A30] text-white text-[9px] font-bold rounded-full">
                  {badgeCount > 9 ? "9+" : badgeCount}
                </span>
              )}
            </div>
            <span className="text-[10px] font-medium leading-none">{t.label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
