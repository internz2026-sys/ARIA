"use client";

import React, { useState, useEffect } from "react";
import { supabase } from "@/lib/supabase";
import { API_URL } from "@/lib/api";

const steps = [
  { num: 1, title: "Chat with ARIA CEO", time: "~5 min", desc: "Describe your product, audience, and marketing goals" },
  { num: 2, title: "Review your GTM profile", time: "~2 min", desc: "Confirm what ARIA captured about your business" },
  { num: 3, title: "Pick your agents", time: "~1 min", desc: "Choose which marketing agents to activate" },
  { num: 4, title: "Launch", time: "~1 min", desc: "ARIA builds your GTM playbook and you're live" },
];

export default function WelcomePage() {
  const [hasExisting, setHasExisting] = useState<boolean | null>(null);
  const [tenantId, setTenantId] = useState<string | null>(null);

  useEffect(() => {
    async function checkExisting() {
      // Check localStorage first
      const storedTenant = localStorage.getItem("aria_tenant_id");
      if (storedTenant) {
        setTenantId(storedTenant);
        setHasExisting(true);
        return;
      }
      // Check server by email
      try {
        const { data: { session } } = await supabase.auth.getSession();
        if (session?.user?.email) {
          const res = await fetch(`${API_URL}/api/tenant/by-email/${encodeURIComponent(session.user.email)}`);
          const data = await res.json();
          if (data.tenant_id) {
            setTenantId(data.tenant_id);
            setHasExisting(true);
            localStorage.setItem("aria_tenant_id", data.tenant_id);
            return;
          }
        }
      } catch {
        // Backend unavailable — fall through
      }
      setHasExisting(false);
    }
    checkExisting();
  }, []);

  function handleRestart() {
    if (tenantId) {
      localStorage.setItem("aria_reonboarding_tenant_id", tenantId);
    }
    window.location.href = "/describe";
  }

  // Still checking
  if (hasExisting === null) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  // Existing user — show restart / edit choice
  if (hasExisting) {
    return (
      <div className="flex flex-col items-center justify-center px-6 py-16 max-w-xl mx-auto text-center">
        <img src="/logo.webp" alt="ARIA" className="h-16 w-16 rounded-full object-cover shadow-lg shadow-[#534AB7]/20 mb-6" />

        <h1 className="text-[28px] font-bold text-[#2C2C2A] mb-3 leading-tight">
          You&apos;ve already completed onboarding.
        </h1>
        <p className="text-[#5F5E5A] text-lg mb-10 max-w-md">
          Would you like to restart onboarding from the beginning or update specific answers?
        </p>

        <div className="w-full max-w-md space-y-3 mb-6">
          {/* Edit specific answers */}
          <a
            href="/edit-profile"
            className="flex items-center gap-4 p-5 rounded-xl border border-[#E0DED8] bg-white hover:border-[#534AB7]/50 hover:bg-[#EEEDFE]/20 transition group cursor-pointer"
          >
            <div className="w-10 h-10 rounded-full bg-[#EEEDFE] flex items-center justify-center flex-shrink-0">
              <svg width="20" height="20" fill="none" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" stroke="#534AB7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" stroke="#534AB7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </div>
            <div className="flex-1 text-left">
              <span className="text-sm font-semibold text-[#2C2C2A] group-hover:text-[#534AB7]">Edit specific answers</span>
              <p className="text-xs text-[#5F5E5A] mt-0.5">Update only the fields you want to change</p>
            </div>
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" stroke="#B0AFA8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </a>

          {/* Restart from scratch */}
          <button
            onClick={handleRestart}
            className="w-full flex items-center gap-4 p-5 rounded-xl border border-[#E0DED8] bg-white hover:border-[#D85A30]/40 hover:bg-[#FDEEE8]/20 transition group cursor-pointer text-left"
          >
            <div className="w-10 h-10 rounded-full bg-[#FDEEE8] flex items-center justify-center flex-shrink-0">
              <svg width="20" height="20" fill="none" viewBox="0 0 24 24"><path d="M1 4v6h6" stroke="#D85A30" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10" stroke="#D85A30" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </div>
            <div className="flex-1">
              <span className="text-sm font-semibold text-[#2C2C2A] group-hover:text-[#D85A30]">Start from scratch</span>
              <p className="text-xs text-[#5F5E5A] mt-0.5">Redo all 8 questions and overwrite your current profile</p>
            </div>
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6" stroke="#B0AFA8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </button>
        </div>

        <a href="/dashboard" className="text-sm text-[#5F5E5A] hover:text-[#2C2C2A] font-medium transition">
          Back to dashboard
        </a>
      </div>
    );
  }

  // New user — standard welcome
  return (
    <div className="flex flex-col items-center justify-center px-6 py-16 max-w-xl mx-auto text-center">
      <img src="/logo.webp" alt="ARIA" className="h-16 w-16 rounded-full object-cover shadow-lg shadow-[#534AB7]/20 mb-6" />

      <h1 className="text-[32px] font-bold text-[#2C2C2A] mb-3 leading-tight">
        Welcome! Let&apos;s set up your<br />AI marketing team.
      </h1>
      <p className="text-[#5F5E5A] text-lg mb-10 max-w-md">
        In a few minutes, ARIA will understand your product and deploy 5 marketing agents tailored to your business.
      </p>

      <div className="w-full max-w-md space-y-3 mb-10 text-left">
        {steps.map((step) => (
          <div key={step.num} className="flex items-start gap-4 p-4 rounded-xl border border-[#E0DED8] bg-white hover:border-[#534AB7]/30 transition">
            <div className="w-8 h-8 rounded-full bg-[#EEEDFE] flex items-center justify-center flex-shrink-0 mt-0.5">
              <span className="text-sm font-bold text-[#534AB7]">{step.num}</span>
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center justify-between">
                <span className="text-sm font-semibold text-[#2C2C2A]">{step.title}</span>
                <span className="text-xs text-[#5F5E5A] ml-2 flex-shrink-0">{step.time}</span>
              </div>
              <p className="text-xs text-[#5F5E5A] mt-0.5">{step.desc}</p>
            </div>
          </div>
        ))}
      </div>

      <p className="text-sm text-[#5F5E5A] mb-6">Total setup time: approximately 10 minutes</p>

      <a
        href="/describe"
        className="inline-flex items-center gap-2 h-12 px-8 rounded-lg bg-[#534AB7] text-white font-semibold text-[15px] hover:bg-[#4840A0] transition shadow-sm"
      >
        Let&apos;s start
        <svg width="18" height="18" fill="none" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
      </a>
    </div>
  );
}
