"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import Link from "next/link";
import { campaigns as campaignsApi } from "@/lib/api";

/* ─── Helpers ─── */

function formatDate(d: string | null | undefined) {
  if (!d) return "—";
  return new Date(d).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function formatCurrency(v: number | null | undefined) {
  if (v == null) return "—";
  return `$${v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

const STATUS_COLORS: Record<string, { bg: string; text: string }> = {
  active: { bg: "bg-green-50", text: "text-green-700" },
  paused: { bg: "bg-yellow-50", text: "text-yellow-700" },
  completed: { bg: "bg-blue-50", text: "text-blue-700" },
  draft: { bg: "bg-gray-50", text: "text-gray-600" },
};

/* ─── Modal ─── */

function Modal({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: React.ReactNode }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <div className="relative bg-white rounded-xl border border-[#E0DED8] shadow-2xl w-full max-w-lg mx-4 max-h-[85vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-[#E0DED8]">
          <h3 className="text-base font-semibold text-[#2C2C2A]">{title}</h3>
          <button onClick={onClose} className="text-[#9E9C95] hover:text-[#2C2C2A]">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>
        <div className="p-6">{children}</div>
      </div>
    </div>
  );
}

const inputCls = "w-full text-sm text-[#2C2C2A] bg-[#F8F8F6] border border-[#E0DED8] rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7]";

/* ─── Upload Flow ─── */

function UploadModal({ open, onClose, tenantId, onSuccess }: { open: boolean; onClose: () => void; tenantId: string; onSuccess: () => void }) {
  const [step, setStep] = useState<"upload" | "associate" | "creating">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [parsed, setParsed] = useState<any>(null);
  const [campaignName, setCampaignName] = useState("");
  const [platform, setPlatform] = useState("facebook");
  const fileRef = useRef<HTMLInputElement>(null);

  const handleUpload = async () => {
    if (!file) return;
    setLoading(true);
    setError("");
    try {
      const result = await campaignsApi.upload(tenantId, file);
      if (result.status === "needs_association") {
        setParsed(result);
        setCampaignName(result.suggestions?.[0]?.parsed_campaign_name || file.name.replace(/\.(csv|tsv|txt|xlsx|xlsm)$/i, ""));
        setStep("associate");
      } else {
        onSuccess();
        onClose();
      }
    } catch (e: any) {
      setError(e.message || "Upload failed");
    } finally {
      setLoading(false);
    }
  };

  const handleCreateAndLink = async () => {
    if (!file) return;
    setLoading(true);
    setError("");
    setStep("creating");
    try {
      await campaignsApi.uploadAndCreate(tenantId, file, campaignName, platform);
      onSuccess();
      onClose();
    } catch (e: any) {
      setError(e.message || "Failed to create campaign");
      setStep("associate");
    } finally {
      setLoading(false);
    }
  };

  const handleLinkToExisting = async (campaignId: string) => {
    if (!file) return;
    setLoading(true);
    setError("");
    try {
      await campaignsApi.upload(tenantId, file, campaignId);
      onSuccess();
      onClose();
    } catch (e: any) {
      setError(e.message || "Failed to link report");
    } finally {
      setLoading(false);
    }
  };

  const reset = () => { setStep("upload"); setFile(null); setParsed(null); setError(""); setCampaignName(""); };

  if (!open) return null;

  return (
    <Modal open={open} onClose={() => { reset(); onClose(); }} title={step === "associate" ? "Link Report to Campaign" : "Upload Facebook Ads Report"}>
      {error && <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{error}</div>}

      {step === "upload" && (
        <div className="space-y-4">
          <p className="text-sm text-[#5F5E5A]">
            Upload a CSV or Excel (.xlsx) export from Facebook Ads Manager. ARIA will parse the metrics and create a campaign report.
          </p>
          <div
            className="border-2 border-dashed border-[#E0DED8] rounded-xl p-8 text-center cursor-pointer hover:border-[#534AB7]/40 transition"
            onClick={() => fileRef.current?.click()}
          >
            <input
              ref={fileRef}
              type="file"
              accept=".csv,.tsv,.txt,.xlsx,.xlsm,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv"
              className="hidden"
              onChange={e => setFile(e.target.files?.[0] || null)}
            />
            {file ? (
              <div>
                <p className="text-sm font-medium text-[#2C2C2A]">{file.name}</p>
                <p className="text-xs text-[#9E9C95] mt-1">{(file.size / 1024).toFixed(0)} KB</p>
              </div>
            ) : (
              <div>
                <svg className="w-10 h-10 mx-auto text-[#9E9C95] mb-2" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" /></svg>
                <p className="text-sm text-[#5F5E5A]">Click to select a CSV file</p>
                <p className="text-xs text-[#9E9C95] mt-1">Facebook Ads Manager export (CSV only)</p>
              </div>
            )}
          </div>
          <button
            disabled={!file || loading}
            onClick={handleUpload}
            className="w-full py-2.5 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition disabled:opacity-40"
          >
            {loading ? "Parsing..." : "Upload & Parse"}
          </button>
        </div>
      )}

      {step === "associate" && parsed && (
        <div className="space-y-4">
          <div className="p-3 bg-[#F8F8F6] rounded-lg">
            <p className="text-xs font-semibold text-[#5F5E5A] mb-1">Parsed from report</p>
            <p className="text-sm font-medium text-[#2C2C2A]">{parsed.suggestions?.[0]?.parsed_campaign_name || "Unknown"}</p>
            <p className="text-xs text-[#9E9C95] mt-1">{parsed.parsed?.row_count} rows • {parsed.parsed?.mapped_columns?.length} metrics detected</p>
          </div>

          {/* Link to existing campaign */}
          {parsed.suggestions?.[0]?.matching_campaigns?.length > 0 && (
            <div>
              <p className="text-xs font-semibold text-[#5F5E5A] mb-2">MATCHING CAMPAIGNS</p>
              <div className="space-y-2">
                {parsed.suggestions[0].matching_campaigns.map((c: any) => (
                  <button
                    key={c.id}
                    onClick={() => handleLinkToExisting(c.id)}
                    disabled={loading}
                    className="w-full text-left p-3 border border-[#E0DED8] rounded-lg hover:border-[#534AB7]/40 transition"
                  >
                    <p className="text-sm font-medium text-[#2C2C2A]">{c.campaign_name}</p>
                    <p className="text-xs text-[#9E9C95]">{c.platform} • {c.status}</p>
                  </button>
                ))}
              </div>
            </div>
          )}

          {/* Create new campaign */}
          <div className="border-t border-[#E0DED8] pt-4">
            <p className="text-xs font-semibold text-[#5F5E5A] mb-2">OR CREATE NEW CAMPAIGN</p>
            <div className="space-y-3">
              <input className={inputCls} placeholder="Campaign name" value={campaignName} onChange={e => setCampaignName(e.target.value)} />
              <select className={inputCls} value={platform} onChange={e => setPlatform(e.target.value)}>
                <option value="facebook">Facebook / Meta</option>
                <option value="instagram">Instagram</option>
                <option value="google">Google Ads</option>
                <option value="linkedin">LinkedIn</option>
                <option value="tiktok">TikTok</option>
              </select>
              <button
                disabled={!campaignName.trim() || loading}
                onClick={handleCreateAndLink}
                className="w-full py-2.5 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition disabled:opacity-40"
              >
                {loading ? "Creating..." : "Create Campaign & Link Report"}
              </button>
            </div>
          </div>
        </div>
      )}

      {step === "creating" && (
        <div className="py-8 text-center">
          <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin mx-auto mb-3" />
          <p className="text-sm text-[#5F5E5A]">Creating campaign and importing report...</p>
        </div>
      )}
    </Modal>
  );
}

/* ─── Create Campaign Modal ─── */

function CreateCampaignModal({ open, onClose, tenantId, onSuccess }: { open: boolean; onClose: () => void; tenantId: string; onSuccess: () => void }) {
  const [name, setName] = useState("");
  const [platform, setPlatform] = useState("facebook");
  const [objective, setObjective] = useState("");
  const [budget, setBudget] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const handleCreate = async () => {
    if (!name.trim()) return;
    setLoading(true);
    setError("");
    try {
      await campaignsApi.create(tenantId, {
        campaign_name: name.trim(),
        platform,
        objective,
        budget: budget ? parseFloat(budget) : undefined,
      });
      onSuccess();
      onClose();
      setName(""); setObjective(""); setBudget("");
    } catch (e: any) {
      setError(e.message || "Failed to create");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal open={open} onClose={onClose} title="New Campaign">
      {error && <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">{error}</div>}
      <div className="space-y-3">
        <div>
          <label className="text-xs font-semibold text-[#5F5E5A]">CAMPAIGN NAME</label>
          <input className={inputCls} placeholder="e.g. Spring Launch Campaign" value={name} onChange={e => setName(e.target.value)} />
        </div>
        <div>
          <label className="text-xs font-semibold text-[#5F5E5A]">PLATFORM</label>
          <select className={inputCls} value={platform} onChange={e => setPlatform(e.target.value)}>
            <option value="facebook">Facebook / Meta</option>
            <option value="instagram">Instagram</option>
            <option value="google">Google Ads</option>
            <option value="linkedin">LinkedIn</option>
            <option value="tiktok">TikTok</option>
          </select>
        </div>
        <div>
          <label className="text-xs font-semibold text-[#5F5E5A]">OBJECTIVE</label>
          <input className={inputCls} placeholder="e.g. Lead generation, Website traffic" value={objective} onChange={e => setObjective(e.target.value)} />
        </div>
        <div>
          <label className="text-xs font-semibold text-[#5F5E5A]">BUDGET (OPTIONAL)</label>
          <input className={inputCls} type="number" placeholder="0.00" value={budget} onChange={e => setBudget(e.target.value)} />
        </div>
        <button
          disabled={!name.trim() || loading}
          onClick={handleCreate}
          className="w-full py-2.5 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition disabled:opacity-40"
        >
          {loading ? "Creating..." : "Create Campaign"}
        </button>
      </div>
    </Modal>
  );
}

/* ─── Main Page ─── */

export default function CampaignsPage() {
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";
  const [data, setData] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [showUpload, setShowUpload] = useState(false);
  const [showCreate, setShowCreate] = useState(false);

  const load = useCallback(async () => {
    if (!tenantId) return;
    setLoading(true);
    try {
      const res = await campaignsApi.list(tenantId, filter);
      setData(res.campaigns || []);
    } catch (e) {
      console.error("Failed to load campaigns", e);
    } finally {
      setLoading(false);
    }
  }, [tenantId, filter]);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="p-6 md:p-8 max-w-6xl mx-auto space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold text-[#2C2C2A]">Campaigns</h1>
          <p className="text-sm text-[#5F5E5A] mt-0.5">Track ad campaigns and upload performance reports</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setShowUpload(true)}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg border border-[#E0DED8] text-[#2C2C2A] hover:bg-[#F8F8F6] transition"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5m-13.5-9L12 3m0 0l4.5 4.5M12 3v13.5" /></svg>
            Upload Report
          </button>
          <button
            onClick={() => setShowCreate(true)}
            className="flex items-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" /></svg>
            New Campaign
          </button>
        </div>
      </div>

      {/* Filters */}
      <div className="flex gap-2">
        {["", "active", "paused", "completed"].map(s => (
          <button
            key={s}
            onClick={() => setFilter(s)}
            className={`px-3 py-1.5 text-xs font-medium rounded-lg transition ${
              filter === s ? "bg-[#534AB7] text-white" : "bg-[#F8F8F6] text-[#5F5E5A] hover:bg-[#E0DED8]"
            }`}
          >
            {s || "All"}
          </button>
        ))}
      </div>

      {/* Campaign List */}
      {loading ? (
        <div className="flex justify-center py-12">
          <div className="w-6 h-6 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
        </div>
      ) : data.length === 0 ? (
        <div className="text-center py-16 bg-white rounded-xl border border-[#E0DED8]">
          <svg className="w-12 h-12 mx-auto text-[#9E9C95] mb-3" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" /></svg>
          <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No campaigns yet</h3>
          <p className="text-sm text-[#5F5E5A] mb-4">Create a campaign or upload a Facebook Ads report to get started.</p>
          <div className="flex justify-center gap-2">
            <button onClick={() => setShowUpload(true)} className="px-4 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] hover:bg-[#F8F8F6] transition">
              Upload Report
            </button>
            <button onClick={() => setShowCreate(true)} className="px-4 py-2 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition">
              New Campaign
            </button>
          </div>
        </div>
      ) : (
        <div className="space-y-3">
          {data.map((c: any) => {
            const sc = STATUS_COLORS[c.status] || STATUS_COLORS.draft;
            return (
              <Link
                key={c.id}
                href={`/campaigns/${c.id}`}
                className="block bg-white rounded-xl border border-[#E0DED8] p-5 hover:border-[#534AB7]/30 hover:shadow-sm transition"
              >
                <div className="flex items-start justify-between">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <h3 className="text-sm font-semibold text-[#2C2C2A] truncate">{c.campaign_name}</h3>
                      <span className={`px-2 py-0.5 text-[10px] font-semibold rounded-full ${sc.bg} ${sc.text} uppercase`}>
                        {c.status}
                      </span>
                    </div>
                    <div className="flex items-center gap-3 text-xs text-[#9E9C95]">
                      <span className="capitalize">{c.platform}</span>
                      {c.objective && <><span>•</span><span>{c.objective}</span></>}
                      {c.budget && <><span>•</span><span>Budget: {formatCurrency(c.budget)}</span></>}
                    </div>
                  </div>
                  <div className="text-right shrink-0 ml-4">
                    {c.latest_report_date ? (
                      <div>
                        <p className="text-[10px] text-[#9E9C95] uppercase">Last report</p>
                        <p className="text-xs font-medium text-[#2C2C2A]">{formatDate(c.latest_report_date)}</p>
                      </div>
                    ) : (
                      <p className="text-xs text-[#9E9C95]">No reports yet</p>
                    )}
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}

      {/* Modals */}
      <UploadModal open={showUpload} onClose={() => setShowUpload(false)} tenantId={tenantId} onSuccess={load} />
      <CreateCampaignModal open={showCreate} onClose={() => setShowCreate(false)} tenantId={tenantId} onSuccess={load} />
    </div>
  );
}
