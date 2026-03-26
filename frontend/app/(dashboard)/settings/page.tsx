"use client";

import React, { useState, useEffect } from "react";
import { supabase } from "@/lib/supabase";

const settingsTabs = ["Profile", "Integrations", "Notifications", "Billing"];

const integrations = [
  { name: "Mailchimp", connected: false, description: "Email marketing automation", phase: "v1.5" },
  { name: "ConvertKit", connected: false, description: "Creator email marketing", phase: "v1.5" },
  { name: "X / Twitter", connected: false, description: "Social media publishing", phase: "v2" },
  { name: "LinkedIn", connected: false, description: "Professional social publishing", phase: "v2" },
  { name: "Meta Ads", connected: false, description: "Automated ad campaign management", phase: "v2.5" },
  { name: "Google Analytics", connected: false, description: "Traffic and conversion analytics", phase: "v3" },
];

export default function SettingsPage() {
  const [activeTab, setActiveTab] = useState("Profile");
  const [user, setUser] = useState({ name: "", email: "", company: "" });

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
  }, []);

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
        <div className="bg-white rounded-xl border border-[#E0DED8]">
          <div className="px-5 py-4 border-b border-[#E0DED8]">
            <h2 className="text-base font-semibold text-[#2C2C2A]">Integrations</h2>
            <p className="text-xs text-[#5F5E5A] mt-1">ARIA v1 uses a copy-paste model. Direct integrations are coming in future versions.</p>
          </div>
          <div className="divide-y divide-[#E0DED8]">
            {integrations.map((int) => (
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
