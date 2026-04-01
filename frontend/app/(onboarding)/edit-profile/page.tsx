"use client";

import React, { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { API_URL } from "@/lib/api";

const CHANNEL_OPTIONS = ["email", "social", "ads", "content"];
const VOICE_OPTIONS = ["professional", "friendly", "bold", "luxury", "casual"];

interface OnboardingData {
  business_name: string;
  offer: string;
  target_audience: string;
  problem_solved: string;
  differentiator: string;
  channels: string[];
  brand_voice: string;
  thirty_day_goal: string;
}

const FIELD_META: { key: keyof OnboardingData; label: string; question: string }[] = [
  { key: "business_name", label: "Business name", question: "What is your business or brand name?" },
  { key: "offer", label: "Product / offer", question: "What product, service, or offer do you sell?" },
  { key: "target_audience", label: "Target audience", question: "Who is your ideal customer?" },
  { key: "problem_solved", label: "Problem solved", question: "What main problem does your offer solve?" },
  { key: "differentiator", label: "Differentiator", question: "What makes your offer different from competitors?" },
  { key: "channels", label: "Channels", question: "Which channels should ARIA focus on?" },
  { key: "brand_voice", label: "Brand voice", question: "What tone should ARIA use for your brand?" },
  { key: "thirty_day_goal", label: "30-day goal", question: "What is your main goal for the next 30 days?" },
];

export default function EditProfilePage() {
  const router = useRouter();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState(false);
  const [data, setData] = useState<OnboardingData>({
    business_name: "",
    offer: "",
    target_audience: "",
    problem_solved: "",
    differentiator: "",
    channels: [],
    brand_voice: "",
    thirty_day_goal: "",
  });
  const [editing, setEditing] = useState<Set<string>>(new Set());
  const [tenantId, setTenantId] = useState<string | null>(null);

  useEffect(() => {
    const tid = localStorage.getItem("aria_tenant_id");
    if (!tid) {
      router.push("/welcome");
      return;
    }
    setTenantId(tid);

    fetch(`${API_URL}/api/tenant/${tid}/onboarding-data`)
      .then(r => r.json())
      .then(d => {
        setData({
          business_name: d.business_name || "",
          offer: d.offer || "",
          target_audience: d.target_audience || "",
          problem_solved: d.problem_solved || "",
          differentiator: d.differentiator || "",
          channels: d.channels || [],
          brand_voice: d.brand_voice || "",
          thirty_day_goal: d.thirty_day_goal || "",
        });
        setLoading(false);
      })
      .catch(() => {
        setError("Could not load your business profile.");
        setLoading(false);
      });
  }, [router]);

  function toggleEdit(key: string) {
    setEditing(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  function toggleChannel(ch: string) {
    setData(prev => ({
      ...prev,
      channels: prev.channels.includes(ch)
        ? prev.channels.filter(c => c !== ch)
        : [...prev.channels, ch],
    }));
  }

  async function handleSave() {
    if (!tenantId || editing.size === 0) return;
    setSaving(true);
    setError("");

    // Only send fields the user actually edited
    const updates: Record<string, any> = {};
    editing.forEach(key => {
      const k = key as keyof OnboardingData;
      updates[k] = data[k];
    });

    try {
      const res = await fetch(`${API_URL}/api/tenant/${tenantId}/update-onboarding`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updates),
      });
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Failed to update profile");
      }
      setSuccess(true);
      setEditing(new Set());
      setTimeout(() => router.push("/dashboard"), 1200);
    } catch (err: any) {
      setError(err.message || "Something went wrong.");
    }
    setSaving(false);
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
          <p className="text-sm text-[#5F5E5A]">Loading your business profile...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-2xl mx-auto px-6 py-10">
      <div className="text-center mb-8">
        <div className="w-12 h-12 rounded-full bg-[#EEEDFE] flex items-center justify-center mx-auto mb-4">
          <svg width="22" height="22" fill="none" viewBox="0 0 24 24"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" stroke="#534AB7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" stroke="#534AB7" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </div>
        <h1 className="text-[28px] font-bold text-[#2C2C2A] mb-2">Edit your business profile</h1>
        <p className="text-[#5F5E5A] text-[15px]">Click the edit button on any field you want to change. Unchanged fields stay as they are.</p>
      </div>

      {error && (
        <div className="mb-4 p-3 rounded-lg bg-red-50 border border-red-200 text-sm text-red-700 text-center">{error}</div>
      )}

      {success && (
        <div className="mb-4 p-3 rounded-lg bg-[#E6F7F0] border border-[#1D9E75]/20 text-sm text-[#1D9E75] text-center font-medium">
          Profile updated! Redirecting to dashboard...
        </div>
      )}

      <div className="space-y-3">
        {FIELD_META.map(({ key, label, question }) => {
          const isEditing = editing.has(key);
          const value = key === "channels"
            ? (data.channels.length > 0 ? data.channels.join(", ") : "Not specified")
            : (data[key] || "Not specified");
          const isEmpty = key === "channels" ? data.channels.length === 0 : !data[key];

          return (
            <div key={key} className={`bg-white rounded-xl border overflow-hidden transition-colors ${isEditing ? "border-[#534AB7] ring-1 ring-[#534AB7]/20" : "border-[#E0DED8]"}`}>
              <div className="flex items-center justify-between px-5 py-3.5">
                <div className="flex-1 min-w-0">
                  <span className="text-xs text-[#5F5E5A] uppercase tracking-wide font-medium">{label}</span>
                  {!isEditing && (
                    <p className={`text-sm mt-0.5 ${isEmpty ? "text-[#B0AFA8] italic" : "text-[#2C2C2A]"}`}>{value}</p>
                  )}
                </div>
                <button
                  onClick={() => toggleEdit(key)}
                  className={`text-xs font-medium px-3 py-1.5 rounded-lg transition-colors flex-shrink-0 ml-3 ${
                    isEditing
                      ? "bg-[#534AB7] text-white"
                      : "border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] hover:text-[#2C2C2A]"
                  }`}
                >
                  {isEditing ? "Done" : "Edit"}
                </button>
              </div>

              {isEditing && (
                <div className="px-5 pb-4">
                  <p className="text-xs text-[#5F5E5A] mb-2">{question}</p>

                  {key === "channels" ? (
                    <div className="flex flex-wrap gap-2">
                      {CHANNEL_OPTIONS.map(ch => (
                        <button
                          key={ch}
                          onClick={() => toggleChannel(ch)}
                          className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                            data.channels.includes(ch)
                              ? "bg-[#534AB7] text-white"
                              : "border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6]"
                          }`}
                        >
                          {ch.charAt(0).toUpperCase() + ch.slice(1)}
                        </button>
                      ))}
                    </div>
                  ) : key === "brand_voice" ? (
                    <div className="flex flex-wrap gap-2">
                      {VOICE_OPTIONS.map(v => (
                        <button
                          key={v}
                          onClick={() => setData(prev => ({ ...prev, brand_voice: v }))}
                          className={`px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                            data.brand_voice === v
                              ? "bg-[#534AB7] text-white"
                              : "border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6]"
                          }`}
                        >
                          {v.charAt(0).toUpperCase() + v.slice(1)}
                        </button>
                      ))}
                    </div>
                  ) : key === "offer" || key === "thirty_day_goal" ? (
                    <textarea
                      value={data[key]}
                      onChange={e => setData(prev => ({ ...prev, [key]: e.target.value }))}
                      rows={3}
                      className="w-full px-3 py-2.5 border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7] resize-none"
                    />
                  ) : (
                    <input
                      type="text"
                      value={data[key] as string}
                      onChange={e => setData(prev => ({ ...prev, [key]: e.target.value }))}
                      className="w-full px-3 py-2.5 border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7]"
                    />
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>

      <div className="flex items-center justify-between mt-8">
        <a href="/welcome" className="text-sm text-[#5F5E5A] hover:text-[#2C2C2A] font-medium flex items-center gap-1.5">
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          Back
        </a>
        <button
          onClick={handleSave}
          disabled={saving || editing.size === 0}
          className="inline-flex items-center gap-2 h-11 px-8 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#4840A0] transition shadow-sm disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {saving ? (
            <>
              <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
              Saving...
            </>
          ) : (
            <>
              Save changes
              <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </>
          )}
        </button>
      </div>

      {editing.size === 0 && !success && (
        <p className="text-[11px] text-[#B0AFA8] text-center mt-3">Click &quot;Edit&quot; on at least one field to enable saving</p>
      )}
    </div>
  );
}
