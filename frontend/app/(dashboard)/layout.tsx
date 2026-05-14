"use client";

import React, { useState, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import { API_URL, authFetch } from "@/lib/api";
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

type AccountStatus = "active" | "paused" | "suspended";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);
  const [accountStatus, setAccountStatus] = useState<AccountStatus>("active");
  // Mobile header auto-hide: visible when scrolling UP, hidden when
  // scrolling DOWN. Twitter-style affordance — gets out of the way
  // while reading, snaps back the moment the user wants to navigate.
  const [hideHeader, setHideHeader] = useState(false);
  const lastScrollYRef = useRef(0);

  useEffect(() => {
    let ticking = false;
    const THRESHOLD = 8;     // ignore sub-pixel jitter
    const TOP_ZONE = 56;     // always show in the top 56px (one header height)
    const onScroll = () => {
      if (ticking) return;
      ticking = true;
      requestAnimationFrame(() => {
        const y = window.scrollY || document.documentElement.scrollTop || 0;
        const last = lastScrollYRef.current;
        if (y < TOP_ZONE) {
          // Near the top — always show. Avoids the header staying
          // hidden when the user is bouncing the rubber-band scroll.
          setHideHeader(false);
        } else if (y - last > THRESHOLD) {
          // Scrolled DOWN past the threshold — hide.
          setHideHeader(true);
        } else if (last - y > THRESHOLD) {
          // Scrolled UP past the threshold — show.
          setHideHeader(false);
        }
        lastScrollYRef.current = y;
        ticking = false;
      });
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

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
        // Unauthenticated users land on the marketing page, not the
        // login form — matches the explicit sign-out destination set
        // in sidebar.tsx / settings/page.tsx. The landing nav still
        // exposes a Login link one click away.
        router.replace("/");
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
          // /api/tenant/by-email/ is no longer public — it requires a JWT
          // whose email claim matches the requested address. authFetch
          // adds the Bearer header from the active Supabase session.
          const res = await authFetch(`${API_URL}/api/tenant/by-email/${encodeURIComponent(email)}`);
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
        // This listener fires immediately after sidebar/settings
        // signOut() — its router.replace would beat the sign-out
        // handler's own redirect to '/' otherwise, dumping the user
        // on /login. Same destination here so both paths agree.
        router.replace("/");
      }
    });

    return () => subscription.unsubscribe();
  }, [router]);

  // Poll the user's account status so the banner reflects pauses
  // applied while the dashboard is open. 60s cadence matches the
  // backend's profile-status cache TTL — anything faster just hits
  // the cache anyway.
  //
  // If /api/profile/me returns 403 with detail "BANNED", the user's
  // account was banned mid-session — redirect to /banned immediately.
  useEffect(() => {
    if (!authChecked) return;
    let cancelled = false;
    const fetchStatus = async () => {
      try {
        const res = await authFetch(`${API_URL}/api/profile/me`);
        if (res.status === 403) {
          // Check if this is a ban signal
          const body = await res.json().catch(() => ({}));
          if (body?.detail === "BANNED") {
            const uid = body?.user_id || session.user?.id;
            if (uid) router.replace(`/banned?user=${encodeURIComponent(uid)}`);
            else router.replace("/banned");
            return;
          }
          return;
        }
        if (!res.ok) return;
        const data = await res.json();
        if (!cancelled && data?.status) {
          setAccountStatus(data.status as AccountStatus);
        }
      } catch {
        // Silent fail — banner stays in its last-known state
      }
    };
    fetchStatus();
    const interval = setInterval(fetchStatus, 60_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [authChecked, router]);

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
            - `bottom` is dynamic: 3.5rem (56px — MobileBottomNav's
              content height) + env(safe-area-inset-bottom). The plain
              `bottom-14` (used originally) ignored the safe-area inset,
              leaving a visible sliver between the sidebar's lower edge
              and the bottom nav on phones with home-indicator insets.
              Aligning the sidebar's bottom to the nav's *top* edge
              closes the gap WITHOUT covering the user profile row at
              the foot of the sidebar.
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
          style={{ bottom: "calc(3.5rem + env(safe-area-inset-bottom, 0px))" }}
          className={`fixed top-0 left-0 z-[55] w-[240px] transform transition-transform duration-200 ease-in-out lg:hidden ${
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
          {/* Mobile header — fixed (not sticky) so it stays pinned even
              when overflow-x:hidden on body creates a new scroll
              container and breaks position:sticky on descendants.
              A matching h-14 spacer below pushes page content clear. */}
          <div
            className={`lg:hidden fixed top-0 left-0 right-0 z-30 bg-white border-b border-[#E0DED8] h-14 flex items-center px-4 transition-transform duration-200 ease-out ${
              hideHeader ? "-translate-y-full" : "translate-y-0"
            }`}
          >
            <button
              onClick={() => setSidebarOpen(true)}
              className="p-2 rounded-lg text-[#5F5E5A] hover:bg-[#F8F8F6] hover:text-[#2C2C2A] transition-colors"
            >
              <svg className="w-6 h-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
              </svg>
            </button>
            <div className="ml-3 flex items-center flex-1">
              <img src="/logo.png" alt="ARIA" className="h-9 w-9 rounded-full object-cover" />
            </div>
            <NotificationBell />
          </div>
          {/* Spacer so page content isn't hidden under the fixed header on mobile */}
          <div className="lg:hidden h-14" aria-hidden="true" />

          {/* Desktop header with notification bell */}
          <div className="hidden lg:flex sticky top-0 z-30 bg-[#F8F8F6] h-14 items-center justify-end px-8 border-b border-[#E0DED8]/50">
            <NotificationBell />
          </div>

          {/* Account-paused banner — persistent until the admin
              flips status back to active. Shown for both 'paused' and
              'suspended' so the user always understands why agent
              actions are disabled, with copy that escalates for
              suspension. */}
          {accountStatus !== "active" && (
            <div className={`px-6 lg:px-8 py-3 border-b ${
              accountStatus === "suspended"
                ? "bg-[#FDEEE8] border-[#D85A30]/30 text-[#B8491F]"
                : "bg-[#FFF4D6] border-[#D4B24C]/40 text-[#8A6D00]"
            }`}>
              <div className="flex items-start gap-3 max-w-screen-2xl mx-auto">
                <svg className="w-5 h-5 flex-shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
                <div className="text-sm">
                  <p className="font-semibold">
                    {accountStatus === "suspended"
                      ? "Your account is suspended."
                      : "Your account is currently paused due to high usage."}
                  </p>
                  <p className="opacity-90 mt-0.5">
                    {accountStatus === "suspended"
                      ? "New agent tasks are disabled. Please contact support to restore access."
                      : "New agent tasks are temporarily disabled. You can still view your existing inbox and history."}
                  </p>
                </div>
              </div>
            </div>
          )}

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
