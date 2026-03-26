"use client";

import React from "react";

export default function AuthLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-white flex flex-col">
      {/* ARIA Logo */}
      <div className="w-full flex justify-center pt-8 pb-4">
        <a href="/" className="flex items-center gap-2">
          <div className="w-9 h-9 rounded-lg bg-[#534AB7] flex items-center justify-center">
            <span className="text-white font-bold text-lg tracking-tight">A</span>
          </div>
          <span className="text-[22px] font-bold text-[#2C2C2A] tracking-tight">ARIA</span>
        </a>
      </div>

      {/* Page content */}
      <div className="flex-1 flex items-start justify-center">
        {children}
      </div>

      {/* Footer */}
      <div className="py-6 text-center text-xs text-[#5F5E5A]">
        &copy; {new Date().getFullYear()} ARIA. All rights reserved.
      </div>
    </div>
  );
}
