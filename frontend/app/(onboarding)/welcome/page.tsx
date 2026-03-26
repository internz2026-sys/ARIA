"use client";

import React from "react";

const steps = [
  { num: 1, title: "Chat with ARIA CEO", time: "~5 min", desc: "Describe your product, audience, and marketing goals" },
  { num: 2, title: "Review your GTM profile", time: "~2 min", desc: "Confirm what ARIA captured about your business" },
  { num: 3, title: "Pick your agents", time: "~1 min", desc: "Choose which marketing agents to activate" },
  { num: 4, title: "Launch", time: "~1 min", desc: "ARIA builds your GTM playbook and you're live" },
];

export default function WelcomePage() {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-16 max-w-xl mx-auto text-center">
      <div className="w-16 h-16 rounded-full bg-[#534AB7] flex items-center justify-center mb-6 shadow-lg shadow-[#534AB7]/20">
        <span className="text-white text-2xl font-bold">A</span>
      </div>

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
