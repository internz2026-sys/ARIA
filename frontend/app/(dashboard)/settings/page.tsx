"use client";

import React, { useState, useEffect, useCallback } from "react";
import { supabase } from "@/lib/supabase";
import { usage as usageApi } from "@/lib/api";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const settingsTabs = ["Profile", "Integrations", "Usage", "Notifications", "Billing"];

const AGENT_LABELS: Record<string, { name: string; color: string }> = {
  ceo: { name: "CEO Strategist", color: "#534AB7" },
  content_writer: { name: "Content Writer", color: "#1D9E75" },
  email_marketer: { name: "Email Marketer", color: "#BA7517" },
  social_manager: { name: "Social Manager", color: "#D85A30" },
  ad_strategist: { name: "Ad Strategist", color: "#5F5E5A" },
};

function UsageBar({ used, limit, color = "#534AB7" }: { used: number; limit: number; color?: string }) {
  const pct = limit > 0 ? Math.min((used / limit) * 100, 100) : 0;
  const isHigh = pct >= 80;
  return (
    <div>
      <div className="h-2 bg-[#F0EFEC] rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all duration-500" style={{ width: `${pct}%`, backgroundColor: isHigh ? "#D85A30" : color }} />
      </div>
      <p className="text-[10px] text-[#9E9C95] mt-1">{used.toLocaleString()} / {limit.toLocaleString()} ({Math.round(pct)}%)</p>
    </div>
  );
}

function UsageDashboard() {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchUsage = useCallback(async () => {
    if (!tenantId) return;
    try {
      const d = await usageApi.getDashboard(tenantId);
      setData(d);
    } catch {} finally { setLoading(false); }
  }, [tenantId]);

  useEffect(() => { fetchUsage(); }, [fetchUsage]);

  // Auto-refresh every 30s
  useEffect(() => {
    const i = setInterval(fetchUsage, 30000);
    return () => clearInterval(i);
  }, [fetchUsage]);

  if (loading) return <div className="p-8 text-center text-sm text-[#9E9C95]">Loading usage data...</div>;
  if (!data) return <div className="p-8 text-center text-sm text-[#9E9C95]">Unable to load usage data.</div>;

  const t = data.tenant || {};
  const agents = data.agents || {};

  return (
    <div className="space-y-4">
      {/* Overall usage */}
      <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-[#2C2C2A]">API Usage This Hour</h2>
          <span className="text-[10px] text-[#9E9C95] bg-[#F8F8F6] px-2.5 py-1 rounded-full">Resets hourly (UTC)</span>
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-6">
          <div>
            <p className="text-xs font-semibold text-[#5F5E5A] mb-2">Requests</p>
            <UsageBar used={t.requests || 0} limit={t.request_limit || 60} />
          </div>
          <div>
            <p className="text-xs font-semibold text-[#5F5E5A] mb-2">Tokens</p>
            <UsageBar used={t.total_tokens || 0} limit={t.token_limit || 200000} />
          </div>
        </div>
        <div className="grid grid-cols-2 gap-4 mt-4 pt-4 border-t border-[#F0EFEC]">
          <div>
            <p className="text-[10px] text-[#9E9C95]">Input tokens</p>
            <p className="text-sm font-semibold text-[#2C2C2A]">{(t.input_tokens || 0).toLocaleString()}</p>
          </div>
          <div>
            <p className="text-[10px] text-[#9E9C95]">Output tokens</p>
            <p className="text-sm font-semibold text-[#2C2C2A]">{(t.output_tokens || 0).toLocaleString()}</p>
          </div>
        </div>
      </div>

      {/* Per-agent breakdown */}
      <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
        <h2 className="text-base font-semibold text-[#2C2C2A] mb-4">Per-Agent Usage & Limits</h2>
        <div className="space-y-5">
          {Object.entries(agents).map(([agentId, a]: [string, any]) => {
            const label = AGENT_LABELS[agentId] || { name: agentId, color: "#5F5E5A" };
            return (
              <div key={agentId} className="border border-[#F0EFEC] rounded-lg p-4">
                <div className="flex items-center gap-2 mb-3">
                  <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: label.color }} />
                  <span className="text-sm font-semibold text-[#2C2C2A]">{label.name}</span>
                  {a.requests > 0 && <span className="text-[10px] text-[#9E9C95] ml-auto">{a.requests} calls this hour</span>}
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <p className="text-[10px] text-[#5F5E5A] mb-1">Requests</p>
                    <UsageBar used={a.requests || 0} limit={a.request_limit || 15} color={label.color} />
                  </div>
                  <div>
                    <p className="text-[10px] text-[#5F5E5A] mb-1">Tokens</p>
                    <UsageBar used={a.total_tokens || 0} limit={a.token_limit || 40000} color={label.color} />
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

const futureIntegrations = [
  { name: "Mailchimp", description: "Email marketing automation", phase: "v1.5" },
  { name: "ConvertKit", description: "Creator email marketing", phase: "v1.5" },
  { name: "X / Twitter", description: "Social media publishing", phase: "v2" },
  { name: "LinkedIn", description: "Professional social publishing", phase: "v2" },
  { name: "Meta Ads", description: "Automated ad campaign management", phase: "v2.5" },
  { name: "Google Analytics", description: "Traffic and conversion analytics", phase: "v3" },
];

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState("Profile");
  const [user, setUser] = useState({ name: "", email: "", company: "" });
  const [gmailConnected, setGmailConnected] = useState<boolean | null>(null);
  const [gmailReconnecting, setGmailReconnecting] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (session?.user) {
        const meta = session.user.user_metadata;
        setUser({
          name: meta?.full_name || meta?.name || "",
          email: session.user.email || "",
          company: meta?.company || "",
        });
      }
    });
    // Check Gmail connection status
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (tenantId) {
      fetch(`${API_URL}/api/integrations/${tenantId}/gmail-status`)
        .then(r => r.json())
        .then(data => setGmailConnected(!!data?.connected))
        .catch(() => setGmailConnected(false));
    }
  }, []);

  async function reconnectGmail() {
    setGmailReconnecting(true);
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/auth/callback?mode=login`,
        scopes: "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly",
        queryParams: { access_type: "offline", prompt: "consent" },
      },
    });
    if (error) setGmailReconnecting(false);
  }

  return (
    <div className="max-w-[900px] space-y-6">
      <h1 className="text-2xl font-semibold text-[#2C2C2A]">Settings</h1>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-[#E0DED8]">
        {settingsTabs.map((tab) => (
          <button key={tab} onClick={() => setActiveTab(tab)} className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px ${activeTab === tab ? "border-[#534AB7] text-[#534AB7]" : "border-transparent text-[#5F5E5A] hover:text-[#2C2C2A]"}`}>
            {tab}
          </button>
        ))}
      </div>

      {/* Profile */}
      {activeTab === "Profile" && (
        <div className="space-y-6">
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-4">Your Profile</h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Full Name</label>
                <input type="text" value={user.name} onChange={(e) => setUser({ ...user, name: e.target.value })} className="w-full px-3 py-2.5 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7]" />
              </div>
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Email</label>
                <input type="email" value={user.email} disabled className="w-full px-3 py-2.5 bg-[#F8F8F6] border border-[#E0DED8] rounded-lg text-sm text-[#5F5E5A] cursor-not-allowed" />
              </div>
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Company / Product</label>
                <input type="text" value={user.company} onChange={(e) => setUser({ ...user, company: e.target.value })} className="w-full px-3 py-2.5 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7]" />
              </div>
            </div>
            <button className="mt-4 px-4 py-2 bg-[#534AB7] text-white rounded-lg text-sm font-medium hover:bg-[#433AA0] transition-colors">Save changes</button>
          </div>

          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-2">Business Profile</h2>
            <p className="text-sm text-[#5F5E5A] mb-4">Edit or redo your onboarding to update how ARIA markets your product.</p>
            <div className="flex items-center gap-3">
              <a href="/edit-profile" className="px-4 py-2 bg-[#534AB7] text-white rounded-lg text-sm font-medium hover:bg-[#433AA0] transition-colors">
                Edit answers
              </a>
              <a href="/welcome" className="px-4 py-2 border border-[#E0DED8] text-[#5F5E5A] rounded-lg text-sm font-medium hover:bg-[#F8F8F6] transition-colors">
                Restart onboarding
              </a>
            </div>
          </div>

          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-4">Brand Voice</h2>
            <p className="text-sm text-[#5F5E5A] mb-4">Select the tone ARIA uses when creating content for your brand.</p>
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {["Professional", "Friendly", "Direct", "Technical"].map((voice) => (
                <button key={voice} className="py-3 px-4 rounded-lg border border-[#E0DED8] text-sm font-medium text-[#2C2C2A] hover:border-[#534AB7] hover:bg-[#EEEDFE] transition-colors">
                  {voice}
                </button>
              ))}
            </div>
          </div>

          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-2">Account</h2>
            <div className="flex items-center justify-between py-3 border-b border-[#E0DED8]">
              <div>
                <p className="text-sm font-medium text-[#2C2C2A]">Change password</p>
                <p className="text-xs text-[#5F5E5A]">Update your account password</p>
              </div>
              <button className="text-xs font-medium px-4 py-2 border border-[#E0DED8] rounded-lg text-[#5F5E5A] hover:bg-[#F8F8F6]">Change</button>
            </div>
            <div className="flex items-center justify-between py-3">
              <div>
                <p className="text-sm font-medium text-[#D85A30]">Sign out</p>
                <p className="text-xs text-[#5F5E5A]">Sign out of your ARIA account</p>
              </div>
              <button onClick={() => supabase.auth.signOut().then(() => window.location.href = "/login")} className="text-xs font-medium px-4 py-2 border border-[#D85A30] rounded-lg text-[#D85A30] hover:bg-[#FDEEE8]">Sign out</button>
            </div>
          </div>
        </div>
      )}

      {/* Integrations */}
      {activeTab === "Integrations" && (
        <div className="space-y-4">
          {/* Gmail — active integration */}
          <div className="bg-white rounded-xl border border-[#E0DED8]">
            <div className="px-5 py-4 border-b border-[#E0DED8]">
              <h2 className="text-base font-semibold text-[#2C2C2A]">Active Integrations</h2>
            </div>
            <div className="px-5 py-4 flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div className="w-9 h-9 rounded-lg bg-[#FDF3E7] flex items-center justify-center">
                  <svg className="w-5 h-5 text-[#BA7517]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
                  </svg>
                </div>
                <div>
                  <p className="text-sm font-medium text-[#2C2C2A]">Gmail</p>
                  <p className="text-xs text-[#5F5E5A] mt-0.5">
                    {gmailConnected === null ? "Checking..." : gmailConnected
                      ? `Connected — emails sent from ${user.email}`
                      : "Not connected — Email Marketer can only draft, not send"}
                  </p>
                </div>
              </div>
              {gmailConnected === false ? (
                <button
                  onClick={reconnectGmail}
                  disabled={gmailReconnecting}
                  className="text-xs font-medium px-4 py-2 rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors disabled:opacity-60"
                >
                  {gmailReconnecting ? "Connecting..." : "Connect Gmail"}
                </button>
              ) : gmailConnected ? (
                <span className="text-[10px] font-medium px-2.5 py-1 rounded-full bg-[#E6F5ED] text-[#1D9E75]">Connected</span>
              ) : null}
            </div>
          </div>

          {/* Future integrations */}
          <div className="bg-white rounded-xl border border-[#E0DED8]">
            <div className="px-5 py-4 border-b border-[#E0DED8]">
              <h2 className="text-base font-semibold text-[#2C2C2A]">Coming Soon</h2>
              <p className="text-xs text-[#5F5E5A] mt-1">Direct integrations are coming in future versions.</p>
            </div>
            <div className="divide-y divide-[#E0DED8]">
              {futureIntegrations.map((int) => (
                <div key={int.name} className="px-5 py-4 flex items-center justify-between">
                  <div>
                    <p className="text-sm font-medium text-[#2C2C2A]">{int.name}</p>
                    <p className="text-xs text-[#5F5E5A] mt-0.5">{int.description}</p>
                  </div>
                  <span className="text-[10px] font-medium px-2 py-1 rounded-full bg-[#F8F8F6] text-[#5F5E5A]">Coming in {int.phase}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Notifications */}
      {activeTab === "Notifications" && (
        <div className="bg-white rounded-xl border border-[#E0DED8] p-6 space-y-4">
          <h2 className="text-base font-semibold text-[#2C2C2A]">Notification Preferences</h2>
          {[
            { label: "Agent completes a task", desc: "Get notified when content is ready for review" },
            { label: "Weekly summary", desc: "Receive a weekly digest of all agent activity" },
            { label: "Strategy recommendations", desc: "CEO agent suggests changes to your GTM plan" },
          ].map((pref) => (
            <div key={pref.label} className="flex items-center justify-between py-2">
              <div>
                <p className="text-sm font-medium text-[#2C2C2A]">{pref.label}</p>
                <p className="text-xs text-[#5F5E5A]">{pref.desc}</p>
              </div>
              <button className="relative inline-flex h-5 w-9 items-center rounded-full bg-[#1D9E75] transition-colors">
                <span className="inline-block h-3.5 w-3.5 rounded-full bg-white translate-x-[18px] transition-transform" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Usage */}
      {activeTab === "Usage" && <UsageDashboard />}

      {/* Billing */}
      {activeTab === "Billing" && (
        <div className="space-y-6">
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-base font-semibold text-[#2C2C2A]">Current Plan</h2>
                <p className="text-sm text-[#5F5E5A] mt-1">You&apos;re on the <span className="font-semibold text-[#534AB7]">Growth</span> plan</p>
              </div>
              <span className="text-2xl font-bold text-[#2C2C2A]">$149<span className="text-sm font-normal text-[#5F5E5A]">/mo</span></span>
            </div>
            <div className="grid grid-cols-3 gap-4 mt-4">
              {[
                { label: "Content pieces", used: 18, total: 30 },
                { label: "Email sequences", used: 2, total: 3 },
                { label: "Campaign plans", used: 1, total: 3 },
              ].map((u) => (
                <div key={u.label}>
                  <p className="text-xs text-[#5F5E5A] mb-1">{u.label}</p>
                  <div className="h-2 bg-[#F8F8F6] rounded-full overflow-hidden">
                    <div className="h-full bg-[#534AB7] rounded-full" style={{ width: `${(u.used / u.total) * 100}%` }} />
                  </div>
                  <p className="text-[10px] text-[#5F5E5A] mt-1">{u.used} / {u.total} used</p>
                </div>
              ))}
            </div>
            <button className="mt-4 text-xs font-medium text-[#534AB7] hover:underline">Upgrade to Scale ($299/mo)</button>
          </div>
        </div>
      )}
    </div>
  );
}
