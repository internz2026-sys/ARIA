"use client";

/**
 * CampaignPerformanceForm — user-reported performance metrics for an
 * active campaign. Renders one of two states:
 *
 *   - read-only summary of the saved values (with Edit button)
 *   - editable form (Save / Cancel)
 *
 * Persists to `campaigns.metadata.performance` via the
 * `campaigns.updateMetrics` helper. Backend (Coder 2) shallow-merges
 * the metadata block so other keys are preserved.
 *
 * V1: text inputs only (no charts). CTR is auto-derived from the
 * uploaded report's impressions when both clicks + impressions are
 * available — otherwise the user can fill it in manually. CPL is
 * auto-derived from leads + spend when both are available.
 */

import React, { useEffect, useMemo, useState } from "react";
import { campaigns as campaignsApi } from "@/lib/api";
import { useNotifications } from "@/lib/use-notifications";

interface PerfBlock {
  clicks?: number | null;
  leads?: number | null;
  spend?: number | null;
  ctr?: number | null;
  cpl?: number | null;
  notes?: string;
  recorded_at?: string;
}

interface Props {
  tenantId: string;
  campaignId: string;
  /** existing metadata.performance block, or null if never reported */
  initial: PerfBlock | null;
  /** Impressions from the latest uploaded CSV report — used to
   *  auto-compute CTR when the user only enters clicks. Optional. */
  impressionsHint?: number | null;
  /** Called after a successful save so the parent re-fetches the
   *  campaign (and thus the merged metadata.performance). */
  onSaved: () => void;
}

function fmtNum(v: number | null | undefined, digits = 2): string {
  if (v == null || isNaN(v as number)) return "—";
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits });
}
function fmtInt(v: number | null | undefined): string {
  if (v == null || isNaN(v as number)) return "—";
  return Number(v).toLocaleString("en-US");
}
function fmtDateTime(d: string | null | undefined): string {
  if (!d) return "—";
  return new Date(d).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

/**
 * Coerce a (possibly empty) string from a number input into either a
 * number or null. Returns null on empty / "" / NaN so the backend
 * stores explicit nulls instead of bogus 0's.
 */
function parseNumOrNull(s: string): number | null {
  const t = (s ?? "").trim();
  if (!t) return null;
  const n = Number(t);
  return isNaN(n) ? null : n;
}

export default function CampaignPerformanceForm({ tenantId, campaignId, initial, impressionsHint, onSaved }: Props) {
  const { showToast } = useNotifications();

  const [editing, setEditing] = useState<boolean>(!initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  // form state — strings (so empty inputs don't coerce to 0)
  const [clicks, setClicks] = useState("");
  const [leads, setLeads] = useState("");
  const [spend, setSpend] = useState("");
  const [ctr, setCtr] = useState("");
  const [cpl, setCpl] = useState("");
  const [notes, setNotes] = useState("");

  // Hydrate form from the saved block whenever it changes (e.g. parent
  // refetches and a new metadata.performance comes back).
  useEffect(() => {
    setClicks(initial?.clicks != null ? String(initial.clicks) : "");
    setLeads(initial?.leads != null ? String(initial.leads) : "");
    setSpend(initial?.spend != null ? String(initial.spend) : "");
    setCtr(initial?.ctr != null ? String(initial.ctr) : "");
    setCpl(initial?.cpl != null ? String(initial.cpl) : "");
    setNotes(initial?.notes || "");
    setEditing(!initial);
  }, [initial]);

  // Auto-compute CTR if user typed clicks but left CTR blank, and we
  // have an impressions hint from the uploaded report.
  const autoCtr = useMemo<number | null>(() => {
    const c = parseNumOrNull(clicks);
    if (c == null || !impressionsHint || impressionsHint <= 0) return null;
    return Number(((c / impressionsHint) * 100).toFixed(2));
  }, [clicks, impressionsHint]);

  // Auto-compute CPL if leads + spend both provided.
  const autoCpl = useMemo<number | null>(() => {
    const l = parseNumOrNull(leads);
    const s = parseNumOrNull(spend);
    if (l == null || s == null || l <= 0) return null;
    return Number((s / l).toFixed(2));
  }, [leads, spend]);

  const handleSave = async () => {
    setSaving(true);
    setError("");
    try {
      const ctrVal = parseNumOrNull(ctr) ?? autoCtr;
      const cplVal = parseNumOrNull(cpl) ?? autoCpl;
      await campaignsApi.updateMetrics(tenantId, campaignId, {
        clicks: parseNumOrNull(clicks),
        leads: parseNumOrNull(leads),
        spend: parseNumOrNull(spend),
        ctr: ctrVal,
        cpl: cplVal,
        notes: notes.trim() || "",
      });
      showToast({ title: "Performance saved", variant: "success" });
      setEditing(false);
      onSaved();
    } catch (e: any) {
      setError(e?.message || "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    // Reset back to the saved values
    setClicks(initial?.clicks != null ? String(initial.clicks) : "");
    setLeads(initial?.leads != null ? String(initial.leads) : "");
    setSpend(initial?.spend != null ? String(initial.spend) : "");
    setCtr(initial?.ctr != null ? String(initial.ctr) : "");
    setCpl(initial?.cpl != null ? String(initial.cpl) : "");
    setNotes(initial?.notes || "");
    setError("");
    if (initial) setEditing(false);
  };

  /* ─── Read-only summary ─── */
  if (!editing && initial) {
    return (
      <div className="bg-white rounded-xl border border-[#E0DED8] p-5 space-y-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h3 className="text-sm font-semibold text-[#2C2C2A]">Reported Performance</h3>
            <p className="text-[10px] text-[#9E9C95] mt-0.5">
              Last updated {fmtDateTime(initial.recorded_at)}
            </p>
          </div>
          <button
            onClick={() => setEditing(true)}
            className="px-3 py-1.5 text-xs font-medium rounded-lg border border-[#E0DED8] text-[#534AB7] hover:bg-[#F8F8F6] transition"
          >
            Edit
          </button>
        </div>
        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
          <SummaryCell label="Clicks" value={fmtInt(initial.clicks)} />
          <SummaryCell label="Leads" value={fmtInt(initial.leads)} />
          <SummaryCell label="Spend" value={initial.spend != null ? `$${fmtNum(initial.spend)}` : "—"} />
          <SummaryCell label="CTR" value={initial.ctr != null ? `${fmtNum(initial.ctr)}%` : "—"} />
          <SummaryCell label="CPL" value={initial.cpl != null ? `$${fmtNum(initial.cpl)}` : "—"} />
        </div>
        {initial.notes && (
          <div className="pt-3 border-t border-[#E0DED8]">
            <p className="text-[10px] uppercase tracking-wide text-[#9E9C95] font-semibold mb-1">Notes</p>
            <p className="text-sm text-[#2C2C2A] whitespace-pre-wrap">{initial.notes}</p>
          </div>
        )}
      </div>
    );
  }

  /* ─── Edit form ─── */
  return (
    <div className="bg-white rounded-xl border border-[#E0DED8] p-5 space-y-4">
      <div>
        <h3 className="text-sm font-semibold text-[#2C2C2A]">Report Performance</h3>
        <p className="text-xs text-[#9E9C95] mt-0.5">
          Tell ARIA how this campaign performed. Leave fields blank if you don't have a value yet.
        </p>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <NumField label="Clicks" value={clicks} onChange={setClicks} placeholder="e.g. 1200" />
        <NumField label="Leads / Sign-ups" value={leads} onChange={setLeads} placeholder="e.g. 32" />
        <NumField label="Spend (USD)" value={spend} onChange={setSpend} placeholder="e.g. 250.00" prefix="$" step="0.01" />
        <NumField
          label="CTR (%)"
          value={ctr}
          onChange={setCtr}
          placeholder={autoCtr != null ? `auto: ${autoCtr}%` : "e.g. 2.5"}
          step="0.01"
          hint={autoCtr != null && !ctr.trim() ? `Auto from ${impressionsHint} impressions` : undefined}
        />
        <NumField
          label="CPL (USD per lead)"
          value={cpl}
          onChange={setCpl}
          placeholder={autoCpl != null ? `auto: $${autoCpl}` : "e.g. 7.81"}
          prefix="$"
          step="0.01"
          hint={autoCpl != null && !cpl.trim() ? "Auto from spend / leads" : undefined}
        />
      </div>

      <div>
        <label className="text-[10px] uppercase tracking-wide text-[#9E9C95] font-semibold">Notes</label>
        <textarea
          rows={3}
          value={notes}
          onChange={(e) => setNotes(e.target.value)}
          placeholder="Anything ARIA should know about how this campaign performed?"
          className="mt-1 w-full px-3 py-2 text-sm rounded-lg border border-[#E0DED8] bg-white text-[#2C2C2A] focus:outline-none focus:border-[#534AB7]"
        />
      </div>

      {error && <p className="text-xs text-red-600">{error}</p>}

      <div className="flex flex-col sm:flex-row sm:justify-end gap-2 pt-1">
        {initial && (
          <button
            onClick={handleCancel}
            disabled={saving}
            className="px-4 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition disabled:opacity-50"
          >
            Cancel
          </button>
        )}
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-4 py-2 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition disabled:opacity-50 flex items-center justify-center gap-2"
        >
          {saving ? (
            <>
              <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
              Saving...
            </>
          ) : (
            "Save Performance"
          )}
        </button>
      </div>
    </div>
  );
}

/* ─── Sub: read-only summary cell ─── */
function SummaryCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="bg-[#F8F8F6] rounded-lg p-3">
      <p className="text-[10px] text-[#9E9C95] uppercase font-medium">{label}</p>
      <p className="text-base font-bold text-[#2C2C2A] mt-0.5">{value}</p>
    </div>
  );
}

/* ─── Sub: numeric input row ─── */
function NumField({
  label,
  value,
  onChange,
  placeholder,
  prefix,
  step,
  hint,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  prefix?: string;
  step?: string;
  hint?: string;
}) {
  return (
    <div>
      <label className="text-[10px] uppercase tracking-wide text-[#9E9C95] font-semibold">{label}</label>
      <div className="mt-1 relative">
        {prefix && (
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-sm text-[#9E9C95] pointer-events-none">
            {prefix}
          </span>
        )}
        <input
          type="number"
          inputMode="decimal"
          step={step || "1"}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          className={`w-full ${prefix ? "pl-7" : "pl-3"} pr-3 py-2 text-sm rounded-lg border border-[#E0DED8] bg-white text-[#2C2C2A] focus:outline-none focus:border-[#534AB7]`}
        />
      </div>
      {hint && <p className="text-[10px] text-[#534AB7] mt-1">{hint}</p>}
    </div>
  );
}
