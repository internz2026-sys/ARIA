"use client";

import React, { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import { API_URL } from "@/lib/api";
import Sidebar from "@/components/shared/sidebar";
import FloatingChat from "@/components/shared/FloatingChat";
import OfficeKanban from "@/components/virtual-office/OfficeKanban";
import NotificationBell from "@/components/shared/NotificationBell";
import ToastContainer from "@/components/shared/ToastContainer";
import { CeoChatProvider } from "@/lib/use-ceo-chat";
import { NotificationProvider } from "@/lib/use-notifications";
import { OfficeAgentsProvider } from "@/lib/use-office-agents";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [authChecked, setAuthChecked] = useState(false);

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
      <div className="min-h-screen bg-[#F8F8F6] flex items-center justify-center">
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
      <div className="min-h-screen bg-[#F8F8F6] flex">
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

        {/* Sidebar - mobile */}
        <div
          className={`fixed inset-y-0 left-0 z-50 w-[240px] transform transition-transform duration-200 ease-in-out lg:hidden ${
            sidebarOpen ? "translate-x-0" : "-translate-x-full"
          }`}
        >
          <Sidebar />
        </div>

        {/* Main content */}
        <div className="flex-1 lg:ml-[240px] min-h-screen">
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

          {/* Page content */}
          <main className="p-6 lg:p-8 pt-6">{children}</main>
        </div>

        {/* Floating widgets — available on every dashboard page */}
        <FloatingChat />
        <OfficeKanban />
        <ToastContainer />
      </div>
      </OfficeAgentsProvider>
      </NotificationProvider>
    </CeoChatProvider>
  );
}
