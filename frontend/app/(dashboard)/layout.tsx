"use client";

import React, { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import { API_URL } from "@/lib/api";
import Sidebar from "@/components/shared/sidebar";
import FloatingChat from "@/components/shared/FloatingChat";
import OfficeKanban from "@/components/virtual-office/OfficeKanban";
import NotificationBell from "@/components/shared/NotificationBell";
import ToastContainer from "@/components/shared/ToastContainer";
import MobileBottomNav from "@/components/shared/MobileBottomNav";
import { CeoChatProvider } from "@/lib/use-ceo-chat";
import { NotificationProvider } from "@/lib/use-notifications";
import { OfficeAgentsProvider } from "@/lib/use-office-agents";
import { ConfirmProvider } from "@/lib/use-confirm";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);

  // Swipe-to-close gesture state. Tracks the starting touch point so
  // we can compute horizontal delta on touchend. We only act on
  // gestures that are clearly horizontal (>2x as much X as Y travel)
  // so vertical scrolling through the menu items still works.
  const swipeStartRef = useRef<{ x: number; y: number; t: number } | null>(null);
  const sidebarDragXRef = useRef<number>(0);
  const sidebarPanelRef = useRef<HTMLDivElement | null>(null);

  const handleSidebarTouchStart = (e: React.TouchEvent) => {
    if (!e.touches.length) return;
    const t = e.touches[0];
    swipeStartRef.current = { x: t.clientX, y: t.clientY, t: Date.now() };
    sidebarDragXRef.current = 0;
  };

  const handleSidebarTouchMove = (e: React.TouchEvent) => {
    const start = swipeStartRef.current;
    if (!start || !e.touches.length) return;
    const dx = e.touches[0].clientX - start.x;
    const dy = e.touches[0].clientY - start.y;
    // Only follow the finger when the gesture is mostly horizontal +
    // leftward. Otherwise let native vertical scrolling happen.
    if (Math.abs(dx) > Math.abs(dy) * 1.5 && dx < 0) {
      sidebarDragXRef.current = dx;
      if (sidebarPanelRef.current) {
        sidebarPanelRef.current.style.transform = `translateX(${dx}px)`;
        sidebarPanelRef.current.style.transition = "none";
      }
    }
  };

  const handleSidebarTouchEnd = () => {
    const start = swipeStartRef.current;
    swipeStartRef.current = null;
    const dx = sidebarDragXRef.current;
    sidebarDragXRef.current = 0;
    // Restore the transition before snapping back / closing so the
    // animation reads as smooth instead of an instant jump.
    if (sidebarPanelRef.current) {
      sidebarPanelRef.current.style.transition = "";
      sidebarPanelRef.current.style.transform = "";
    }
    if (!start) return;
    const elapsed = Date.now() - start.t;
    // Close if dragged > 60px left, OR a quick flick (>0.5 px/ms)
    // covered at least 25px — matches the "native app" feel.
    if (dx < -60 || (dx < -25 && elapsed < 300 && Math.abs(dx) / Math.max(1, elapsed) > 0.5)) {
      setSidebarOpen(false);
    }
  };

  // Auto-close the sidebar whenever a nav link inside it is tapped.
  // Sidebar component renders next/link <a> elements; event delegation
  // on the wrapper div catches every link without forcing the Sidebar
  // to know about the open/close state.
  const handleSidebarClick = (e: React.MouseEvent) => {
    const link = (e.target as HTMLElement).closest("a");
    if (link && sidebarOpen) {
      setSidebarOpen(false);
    }
  };

  useEffect(() => {
    supabase.auth.getSession().then(async ({ data: { session } }) => {
      if (!session) {
        router.replace("/login");
        return;
      }

      // 1. Check localStorage first (fast path)
      const tenantId = localStorage.getItem("aria_tenant_id");
      if (tenantId) {
        setAuthChecked(true);
        return;
      }

      // 2. localStorage empty — check server by user email
      const email = session.user?.email;
      if (email) {
        try {
          const headers: Record<string, string> = {};
          if (session.access_token) headers["Authorization"] = `Bearer ${session.access_token}`;
          const res = await fetch(`${API_URL}/api/tenant/by-email/${encodeURIComponent(email)}`, { headers });
          const data = await res.json();
          if (data.tenant_id) {
            // Restore tenant_id from server — user already onboarded
            localStorage.setItem("aria_tenant_id", data.tenant_id);
            setAuthChecked(true);
            return;
          }
        } catch {
          // Backend down — fall through
        }
      }

      // 3. No config found anywhere — redirect to onboarding
      router.replace("/welcome");
    });

    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      if (!session) {
        router.replace("/login");
      }
    });

    return () => subscription.unsubscribe();
  }, [router]);

  if (!authChecked) {
    return (
      <div className="min-h-[100dvh] bg-[#F8F8F6] flex items-center justify-center">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-[#5F5E5A]">Loading...</p>
        </div>
      </div>
    );
  }

  return (
    <CeoChatProvider>
      <NotificationProvider>
      <OfficeAgentsProvider>
      <ConfirmProvider>
      <div className="min-h-[100dvh] bg-[#F8F8F6] flex">
        {/* Mobile overlay */}
        {sidebarOpen && (
          <div
            className="fixed inset-0 bg-black/30 z-40 lg:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Sidebar - desktop */}
        <div className="hidden lg:block w-[240px] fixed inset-y-0 left-0 z-50">
          <Sidebar />
        </div>

        {/* Sidebar - mobile.
            - top-0 bottom-14 (instead of inset-y-0) leaves a 56px gap
              at the bottom for the MobileBottomNav so the user can
              still tap Inbox/Chats while the drawer is open.
            - z-[55] sits above the backdrop (z-40) but below
              MobileBottomNav (z-[60]) so the bottom bar wins overlap.
            - Touch handlers implement swipe-to-close (left swipe).
              Vertical-only scroll inside the menu still works because
              we ignore gestures that aren't dominantly horizontal.
            - onClick delegate auto-closes when a nav link is tapped. */}
        <div
          ref={sidebarPanelRef}
          onTouchStart={handleSidebarTouchStart}
          onTouchMove={handleSidebarTouchMove}
          onTouchEnd={handleSidebarTouchEnd}
          onTouchCancel={handleSidebarTouchEnd}
          onClick={handleSidebarClick}
          className={`fixed top-0 bottom-14 left-0 z-[55] w-[240px] transform transition-transform duration-200 ease-in-out lg:hidden ${
            sidebarOpen ? "translate-x-0" : "-translate-x-full"
          }`}
        >
          <Sidebar />
          {/* Subtle right-edge handle — affords the swipe gesture
              without adding visual noise. Mobile-only; hidden on lg+. */}
          {sidebarOpen && (
            <div
              aria-hidden="true"
              className="absolute right-0 top-1/2 -translate-y-1/2 h-12 w-1 rounded-full bg-[#C5C3BC]/60 mr-1"
            />
          )}
        </div>

        {/* Main content. `min-w-0` is critical: a flex child's default
            `min-width: auto` lets it grow to fit its content, which is
            exactly the bug behind "page is wider than the phone screen".
            With `min-w-0` the column can shrink below content size and
            children with `truncate` / `overflow-x-auto` actually clip
            and scroll inside their own bounds instead of expanding the
            whole page rightward. */}
        <div className="flex-1 lg:ml-[240px] min-h-[100dvh] min-w-0">
          {/* Mobile header */}
          <div className="lg:hidden sticky top-0 z-30 bg-white border-b border-[#E0DED8] h-14 flex items-center px-4">
            <button
              onClick={() => setSidebarOpen(true)}
              className="p-2 rounded-lg text-[#5F5E5A] hover:bg-[#F8F8F6] hover:text-[#2C2C2A] transition-colors"
            >
              <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
              </svg>
            </button>
            <div className="ml-3 flex items-center gap-2 flex-1">
              <img src="/logo.webp" alt="ARIA" className="h-7 w-7 rounded-full object-cover" />
              <span className="text-[#2C2C2A] font-semibold text-base">ARIA</span>
            </div>
            <NotificationBell />
          </div>

          {/* Desktop header with notification bell */}
          <div className="hidden lg:flex sticky top-0 z-30 bg-[#F8F8F6] h-14 items-center justify-end px-8 border-b border-[#E0DED8]/50">
            <NotificationBell />
          </div>

          {/* Page content. pb-20 on mobile keeps the final row out
              from under the bottom-tab nav; lg:pb-8 removes the extra
              space on desktop where the nav isn't shown. */}
          <main className="p-6 lg:p-8 pt-6 pb-24 lg:pb-8">{children}</main>
        </div>

        {/* Floating widgets — available on every dashboard page */}
        <FloatingChat />
        <OfficeKanban />
        <ToastContainer />

        {/* Bottom-tab bar for mobile. Hidden on lg+ where the left
            sidebar takes over. The hamburger drawer (top-left) still
            covers secondary routes so no navigation is lost. */}
        <MobileBottomNav />
      </div>
      </ConfirmProvider>
      </OfficeAgentsProvider>
      </NotificationProvider>
    </CeoChatProvider>
  );
}
