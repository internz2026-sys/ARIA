"use client";

import React, { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";

const steps = [
  { path: "/welcome", label: "Welcome" },
  { path: "/describe", label: "Describe" },
  { path: "/edit-profile", label: "Edit" },
  { path: "/review", label: "Review" },
  { path: "/select-agents", label: "Agents" },
  { path: "/connect", label: "Connect" },
];

export default function OnboardingLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [authChecked, setAuthChecked] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (!session) {
        router.replace("/login");
      } else {
        setAuthChecked(true);
      }
    });
  }, [router]);

  const currentIndex = steps.findIndex((s) => pathname.includes(s.path));

  if (!authChecked) {
    return (
      <div className="min-h-screen bg-white flex items-center justify-center">
        <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-white flex flex-col">
      {/* Top bar */}
      <header className="flex items-center justify-between px-6 md:px-10 py-5 border-b border-[#E0DED8]">
        {/* Logo */}
        <a href="/" className="flex items-center gap-2">
          <img src="/logo.png" alt="ARIA" className="h-8 w-8 rounded-full object-cover" />
          <span className="text-lg font-bold text-[#2C2C2A] tracking-tight">ARIA</span>
        </a>

        {/* Progress dots */}
        <div className="flex items-center gap-2">
          {steps.map((step, i) => (
            <div key={step.path} className="flex items-center gap-2">
              <div className="flex flex-col items-center">
                <div
                  className={`w-2.5 h-2.5 rounded-full transition-colors ${
                    i < currentIndex
                      ? "bg-[#1D9E75]"
                      : i === currentIndex
                      ? "bg-[#534AB7] ring-4 ring-[#534AB7]/20"
                      : "bg-[#E0DED8]"
                  }`}
                />
                <span className={`text-[10px] mt-1 hidden sm:block ${
                  i === currentIndex ? "text-[#534AB7] font-semibold" : "text-[#5F5E5A]"
                }`}>
                  {step.label}
                </span>
              </div>
              {i < steps.length - 1 && (
                <div className={`w-8 h-px ${i < currentIndex ? "bg-[#1D9E75]" : "bg-[#E0DED8]"}`} />
              )}
            </div>
          ))}
        </div>

        {/* Save & exit */}
        <a href="/dashboard" className="text-sm text-[#5F5E5A] hover:text-[#2C2C2A] font-medium transition">
          Save &amp; exit
        </a>
      </header>

      {/* Page content */}
      <main className="flex-1">
        {children}
      </main>
    </div>
  );
}
