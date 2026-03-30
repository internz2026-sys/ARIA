"use client";

import React, { useState, useEffect } from "react";
import { useRouter } from "next/navigation";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export default function ReviewPage() {
  const router = useRouter();
  const [config, setConfig] = useState<Record<string, any> | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const sessionId = localStorage.getItem("aria_onboarding_session");
    if (!sessionId) {
      router.push("/describe");
      return;
    }
    fetch(`${API_URL}/api/onboarding/extract-config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    })
      .then(r => r.json())
      .then(data => {
        const cfg = data.config || {};
        setConfig(cfg);
        localStorage.setItem("aria_onboarding_config", JSON.stringify(cfg));
        setLoading(false);
      })
      .catch(() => { setConfig({}); setLoading(false); });
  }, [router]);

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-[#5F5E5A]">ARIA is building your profile...</p>
        </div>
      </div>
    );
  }

  // Helper: extract a display value from multiple possible paths in the config.
  const g = (primary: any, ...fallbacks: any[]) => {
    if (primary && primary !== "") {
      return Array.isArray(primary) ? primary.join(", ") : String(primary);
    }
    for (const fb of fallbacks) {
      if (fb && fb !== "") return Array.isArray(fb) ? fb.join(", ") : String(fb);
    }
    return "Not provided";
  };

  // Flat GTM profile (directly maps to the 8 onboarding answers).
  const gp = config?.gtm_profile || {};

  const sections = [
    {
      title: "Business & Product",
      fields: [
        { label: "Business name", value: g(gp.business_name, config?.business_name) },
        { label: "Product / offer", value: g(gp.offer, config?.product?.description, config?.description) },
        { label: "Differentiator", value: g(gp.differentiator, config?.product?.differentiators) },
      ],
    },
    {
      title: "Target Audience",
      fields: [
        { label: "Audience", value: g(gp.audience, config?.icp?.target_titles, config?.icp?.target_industries) },
        { label: "Problem solved", value: g(gp.problem, config?.icp?.pain_points) },
      ],
    },
    {
      title: "GTM Strategy",
      fields: [
        { label: "Channels", value: g(gp.primary_channels, config?.channels, config?.gtm_playbook?.channel_strategy) },
        { label: "Brand voice", value: g(gp.brand_voice, config?.brand_voice?.tone) },
        { label: "30-day goal", value: g(gp.goal_30_days, config?.gtm_playbook?.action_plan_30) },
      ],
    },
    {
      title: "Generated Strategy",
      fields: [
        { label: "Positioning", value: g(gp.positioning_summary, config?.gtm_playbook?.positioning) },
        { label: "30-day GTM focus", value: g(gp["30_day_gtm_focus"]) },
      ],
    },
  ];

  return (
    <div className="max-w-3xl mx-auto px-6 py-10">
      <div className="text-center mb-8">
        <div className="w-12 h-12 rounded-full bg-[#E6F7F0] flex items-center justify-center mx-auto mb-4">
          <svg width="22" height="22" fill="none" viewBox="0 0 24 24"><path d="M9 11l3 3L22 4" stroke="#1D9E75" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" stroke="#1D9E75" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </div>
        <h1 className="text-[28px] font-bold text-[#2C2C2A] mb-2">Review your profile</h1>
        <p className="text-[#5F5E5A] text-[15px]">ARIA extracted these details from your conversation. Confirm before continuing.</p>
      </div>

      <div className="space-y-4">
        {sections.map((section, i) => (
          <div key={i} className="bg-white rounded-xl border border-[#E0DED8] overflow-hidden">
            <div className="px-6 py-4 border-b border-[#E0DED8] bg-[#F8F8F6]">
              <h3 className="font-semibold text-[15px] text-[#2C2C2A]">{section.title}</h3>
            </div>
            <div className="px-6 py-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-3">
                {section.fields.map((field, j) => (
                  <div key={j}>
                    <span className="text-xs text-[#5F5E5A] uppercase tracking-wide font-medium">{field.label}</span>
                    <p className={`text-sm mt-0.5 ${field.value === "Not provided" ? "text-[#B0AFA8] italic" : "text-[#2C2C2A]"}`}>{field.value}</p>
                  </div>
                ))}
              </div>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between mt-8">
        <a href="/describe" className="text-sm text-[#5F5E5A] hover:text-[#2C2C2A] font-medium flex items-center gap-1.5">
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          Back
        </a>
        <div className="flex items-center gap-3">
          <a href="/edit-profile" className="inline-flex items-center gap-1.5 h-11 px-6 rounded-lg border border-[#534AB7] text-[#534AB7] font-semibold text-sm hover:bg-[#EEEDFE] transition">
            <svg width="14" height="14" fill="none" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            Edit answers
          </a>
          <a href="/select-agents" className="inline-flex items-center gap-2 h-11 px-8 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#4840A0] transition shadow-sm">
            Looks good, continue
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
          </a>
        </div>
      </div>
    </div>
  );
}
