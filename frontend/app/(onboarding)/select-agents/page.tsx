"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import { API_URL, getAuthHeaders } from "@/lib/api";

const agents = [
  { slug: "ceo", name: "ARIA CEO", role: "Chief Marketing Strategist", description: "Builds your GTM playbook, coordinates all agents, reviews outputs, adjusts strategy", color: "#534AB7", required: true },
  { slug: "content_writer", name: "Content Writer", role: "Content Creation", description: "Blog posts, landing pages, Product Hunt copy, Show HN posts, case studies", color: "#1D9E75", required: false },
  { slug: "email_marketer", name: "Email Marketer", role: "Email Campaigns", description: "Welcome sequences, newsletters, launch campaigns, re-engagement emails", color: "#BA7517", required: false },
  { slug: "social_manager", name: "Social Manager", role: "Social Media", description: "X/Twitter, LinkedIn, Facebook posts, content calendar, hashtag strategy", color: "#D85A30", required: false },
  { slug: "ad_strategist", name: "Ad Strategist", role: "Paid Ads", description: "Facebook ad copy, audience targeting, budget allocation, step-by-step guides", color: "#7C3AED", required: false },
  { slug: "media", name: "Media Designer", role: "Visual Content", description: "Marketing images via AI, social media visuals, ad creatives, blog headers", color: "#E4407B", required: false },
];

export default function SelectAgentsPage() {
  const router = useRouter();
  const [enabled, setEnabled] = useState<Record<string, boolean>>(
    Object.fromEntries(agents.map(a => [a.slug, true]))
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  function toggle(slug: string) {
    const agent = agents.find(a => a.slug === slug);
    if (agent?.required) return;
    setEnabled(prev => ({ ...prev, [slug]: !prev[slug] }));
  }

  const activeCount = Object.values(enabled).filter(Boolean).length;

  // Flush Google OAuth tokens saved during signup to the backend
  async function flushGoogleTokens(tenantId: string) {
    const accessToken = localStorage.getItem("aria_google_token");
    if (!accessToken) return;
    const refreshToken = localStorage.getItem("aria_google_refresh_token");
    try {
      const authHeaders = await getAuthHeaders();
      await fetch(`${API_URL}/api/integrations/${tenantId}/google-tokens`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({
          google_access_token: accessToken,
          google_refresh_token: refreshToken || null,
        }),
      });
    } catch {
      // Non-blocking — tokens can be recaptured on next Google login
    } finally {
      localStorage.removeItem("aria_google_token");
      localStorage.removeItem("aria_google_refresh_token");
    }
  }

  async function handleLaunch() {
    setSaving(true);
    setError("");

    try {
      const { data: { session } } = await supabase.auth.getSession();
      const user = session?.user;
      const ownerEmail = user?.email || "";
      const meta = user?.user_metadata || {};
      const ownerName = meta.full_name || meta.name || ownerEmail.split("@")[0] || "";

      const activeAgents = agents.filter(a => enabled[a.slug]).map(a => a.slug);
      const sessionId = localStorage.getItem("aria_onboarding_session");
      const cachedConfig = localStorage.getItem("aria_onboarding_config");
      const skippedTopics = localStorage.getItem("aria_skipped_topics");
      const reonboardingTenantId = localStorage.getItem("aria_reonboarding_tenant_id");

      let res: Response;

      // Try the session-based endpoint first
      if (sessionId) {
        try {
          res = await fetch(`${API_URL}/api/onboarding/save-config`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              session_id: sessionId,
              owner_email: ownerEmail,
              owner_name: ownerName,
              active_agents: activeAgents,
              existing_tenant_id: reonboardingTenantId || undefined,
            }),
          });
        } catch {
          throw new Error("Cannot connect to the backend server. Make sure it is running on " + API_URL);
        }

        // If session is still valid, use the response
        if (res.ok) {
          const data = await res.json();
          localStorage.setItem("aria_tenant_id", data.tenant_id);
          await flushGoogleTokens(data.tenant_id);
          localStorage.removeItem("aria_onboarding_session");
          localStorage.removeItem("aria_onboarding_config");
          localStorage.removeItem("aria_skipped_topics");
          localStorage.removeItem("aria_reonboarding_tenant_id");
          router.push("/dashboard");
          return;
        }
      }

      // Fallback: use cached config from the review page
      if (cachedConfig) {
        try {
          res = await fetch(`${API_URL}/api/onboarding/save-config-direct`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              config: JSON.parse(cachedConfig),
              owner_email: ownerEmail,
              owner_name: ownerName,
              active_agents: activeAgents,
              skipped_topics: skippedTopics ? JSON.parse(skippedTopics) : null,
              existing_tenant_id: reonboardingTenantId || undefined,
            }),
          });
        } catch {
          throw new Error("Cannot connect to the backend server. Make sure it is running on " + API_URL);
        }

        if (!res.ok) {
          const data = await res.json().catch(() => ({}));
          throw new Error(data.detail || "Failed to save configuration");
        }

        const data = await res.json();
        localStorage.setItem("aria_tenant_id", data.tenant_id);
        await flushGoogleTokens(data.tenant_id);
        localStorage.removeItem("aria_onboarding_session");
        localStorage.removeItem("aria_onboarding_config");
        localStorage.removeItem("aria_skipped_topics");
        router.push("/dashboard");
        return;
      }

      // No session and no cached config — must redo onboarding
      setError("Your onboarding session expired. Redirecting to restart...");
      localStorage.removeItem("aria_onboarding_session");
      setTimeout(() => router.push("/describe"), 1500);
    } catch (err: any) {
      setError(err.message || "Something went wrong. Please try again.");
      setSaving(false);
    }
  }

  return (
    <div className="max-w-2xl mx-auto px-6 py-10">
      <div className="text-center mb-8">
        <h1 className="text-[28px] font-bold text-[#2C2C2A] mb-2">Choose your agents</h1>
        <p className="text-[#5F5E5A] text-[15px]">Select which marketing agents to activate. The CEO is always on.</p>
      </div>

      <div className="space-y-3 mb-8">
        {agents.map(agent => (
          <div
            key={agent.slug}
            onClick={() => toggle(agent.slug)}
            className={`flex items-center gap-4 p-4 rounded-xl border transition-all cursor-pointer ${
              enabled[agent.slug]
                ? "border-[#534AB7] bg-[#EEEDFE]/30 ring-1 ring-[#534AB7]/20"
                : "border-[#E0DED8] bg-white hover:border-[#534AB7]/30"
            }`}
          >
            <div className="w-10 h-10 rounded-xl flex items-center justify-center shrink-0" style={{ backgroundColor: agent.color + "18", color: agent.color }}>
              <span className="text-sm font-bold">{agent.name.charAt(0)}</span>
            </div>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2">
                <span className="text-sm font-semibold text-[#2C2C2A]">{agent.name}</span>
                <span className="text-[10px] font-medium px-2 py-0.5 rounded-full" style={{ backgroundColor: agent.color + "18", color: agent.color }}>{agent.role}</span>
                {agent.required && <span className="text-[10px] text-[#5F5E5A] bg-[#F8F8F6] px-2 py-0.5 rounded-full">Required</span>}
              </div>
              <p className="text-xs text-[#5F5E5A] mt-0.5">{agent.description}</p>
            </div>
            <button
              onClick={(e) => { e.stopPropagation(); toggle(agent.slug); }}
              className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ${
                enabled[agent.slug] ? "bg-[#1D9E75]" : "bg-[#E0DED8]"
              } ${agent.required ? "opacity-60 cursor-not-allowed" : ""}`}
            >
              <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${enabled[agent.slug] ? "translate-x-[18px]" : "translate-x-[3px]"}`} />
            </button>
          </div>
        ))}
      </div>

      <p className="text-sm text-[#5F5E5A] text-center mb-6">{activeCount} of {agents.length} agents active</p>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700 text-center">
          {error}
        </div>
      )}

      <div className="flex items-center justify-between">
        <a href="/review" className="text-sm text-[#5F5E5A] hover:text-[#2C2C2A] font-medium flex items-center gap-1.5">
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          Back
        </a>
        <button
          onClick={handleLaunch}
          disabled={saving}
          className="inline-flex items-center gap-2 h-11 px-8 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#4840A0] transition shadow-sm disabled:opacity-60 disabled:cursor-not-allowed"
        >
          {saving ? (
            <>
              <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
              Saving...
            </>
          ) : (
            <>
              Launch ARIA
              <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </>
          )}
        </button>
      </div>
    </div>
  );
}
