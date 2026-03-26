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

  const sections = [
    {
      title: "Product",
      fields: [
        { label: "Name", value: config?.product?.name || config?.business_name || "Not provided" },
        { label: "Description", value: config?.product?.description || config?.description || "Not provided" },
        { label: "Value proposition", value: config?.product?.value_props?.join(", ") || "Not provided" },
        { label: "Type", value: config?.product?.product_type || "Not provided" },
      ],
    },
    {
      title: "Target Audience",
      fields: [
        { label: "Target titles", value: config?.icp?.target_titles?.join(", ") || "Not provided" },
        { label: "Industries", value: config?.icp?.target_industries?.join(", ") || "Not provided" },
        { label: "Pain points", value: config?.icp?.pain_points?.join(", ") || "Not provided" },
      ],
    },
    {
      title: "GTM Strategy",
      fields: [
        { label: "Positioning", value: config?.gtm_playbook?.positioning || "Not provided" },
        { label: "Channels", value: config?.gtm_playbook?.channel_strategy?.join(", ") || config?.channels?.join(", ") || "Not provided" },
        { label: "Messaging pillars", value: config?.gtm_playbook?.messaging_pillars?.join(", ") || "Not provided" },
      ],
    },
    {
      title: "Brand Voice",
      fields: [
        { label: "Tone", value: config?.brand_voice?.tone || "Not provided" },
        { label: "Example phrases", value: config?.brand_voice?.example_phrases?.join(", ") || "Not provided" },
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
        <a href="/select-agents" className="inline-flex items-center gap-2 h-11 px-8 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#4840A0] transition shadow-sm">
          Looks good, continue
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </a>
      </div>
    </div>
  );
}
