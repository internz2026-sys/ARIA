"use client";

/**
 * CampaignCopyPasteTab — renders the Ad Strategist's markdown plan as a
 * copy-paste blueprint (Campaign → Ad Set → Ads) so the user can paste
 * each field into Meta Ads Manager.
 *
 * Lazy-fetches the linked inbox item (via campaign.inbox_item_id) on
 * mount so we don't pay the round-trip until the tab is actually
 * opened. Per-field copy buttons track which variants the user has
 * touched so we can show a "X of N variants pasted" progress chip.
 *
 * The bottom action ("I have pasted this into Meta Ads Manager") flips
 * the campaign to status='active' via the existing PATCH endpoint and
 * persists `pasted_at` + `performance_review_at` into
 * `campaigns.metadata` (server-side shallow-merged) so the badge
 * survives a refresh in any browser. The metadata read on mount is
 * the source of truth; we no longer fall back to localStorage.
 */

import React, { useState, useEffect, useCallback, useMemo } from "react";
import { inbox as inboxApi, campaigns as campaignsApi } from "@/lib/api";

/* ─── Types ─── */

interface AdVariant {
  label: string;
  headline: string;
  primaryText: string;
  description: string;
  ctaButton: string;
}

interface ParsedPlan {
  campaignTitle: string;
  overview: string[];           // raw bullet lines: "Platform: ...", "Objective: ..."
  audience: string[];           // raw bullet lines describing audience
  variants: AdVariant[];
  setupSteps: string[];         // numbered step lines, prefix stripped
}

interface Props {
  tenantId: string;
  campaign: any;                // campaign row (we read .inbox_item_id, .status, .metadata)
  onCampaignUpdate: () => void; // parent refresh hook (so the badge / status updates)
}

/* ─── Markdown parsing ─── */

/**
 * Extract a section between an `## H2` (or `### H3`) heading and the
 * next heading of the same-or-higher level. Returns the inner lines
 * (without the heading itself).
 */
function _extractSection(lines: string[], headingMatcher: RegExp): string[] {
  const startIdx = lines.findIndex((ln) => headingMatcher.test(ln));
  if (startIdx === -1) return [];

  // determine heading level by counting leading '#'
  const headingLine = lines[startIdx];
  const m = headingLine.match(/^(#+)\s/);
  const level = m ? m[1].length : 2;
  // stop on next heading of same or higher level (i.e. fewer-or-equal '#')
  const stopMatcher = new RegExp(`^#{1,${level}}\\s`);

  const out: string[] = [];
  for (let i = startIdx + 1; i < lines.length; i++) {
    if (stopMatcher.test(lines[i])) break;
    out.push(lines[i]);
  }
  return out;
}

function _stripBullet(line: string): string {
  return line.replace(/^\s*[-*]\s+/, "").trim();
}

function _stripBoldLabel(line: string, label: string): string {
  // Match "**Label:** value" or "Label: value" — return the value.
  const re = new RegExp(`^\\s*\\*?\\*?${label}\\*?\\*?\\s*:\\s*`, "i");
  return line.replace(re, "").trim();
}

/**
 * Parse the Ad Strategist's markdown into a structured plan. The
 * agent's prompt fixes the section order (Overview → Target Audience →
 * Ad Creatives → Step-by-Step Setup Guide) so we lean on that, but we
 * still tolerate missing sections — empty arrays are fine, the UI just
 * skips those blocks.
 */
function parsePlan(markdown: string): ParsedPlan {
  const text = markdown || "";
  const lines = text.split(/\r?\n/);

  // Campaign title: "# Campaign: ..." (case-insensitive on "Campaign")
  let campaignTitle = "";
  for (const ln of lines) {
    const m = ln.match(/^#\s+Campaign:\s*(.+?)\s*$/i);
    if (m) { campaignTitle = m[1].trim(); break; }
  }

  // Overview — bullet lines under "## Overview"
  const overviewBlock = _extractSection(lines, /^##\s+Overview\b/i);
  const overview = overviewBlock
    .filter((ln) => ln.trim().startsWith("-") || ln.trim().startsWith("*"))
    .map(_stripBullet)
    .filter(Boolean);

  // Target Audience — bullet lines
  const audienceBlock = _extractSection(lines, /^##\s+Target Audience\b/i);
  const audience = audienceBlock
    .filter((ln) => ln.trim().startsWith("-") || ln.trim().startsWith("*"))
    .map(_stripBullet)
    .filter(Boolean);

  // Ad Variants — under "## Ad Creatives", each variant is a "### Ad Variant N" block
  const creativesBlock = _extractSection(lines, /^##\s+Ad Creatives\b/i);
  const variants: AdVariant[] = [];
  let currentVariant: AdVariant | null = null;
  const flushVariant = () => {
    if (currentVariant && (currentVariant.headline || currentVariant.primaryText)) {
      variants.push(currentVariant);
    }
    currentVariant = null;
  };

  for (const ln of creativesBlock) {
    const variantHeader = ln.match(/^###\s+(.+?)\s*$/);
    if (variantHeader) {
      flushVariant();
      currentVariant = {
        label: variantHeader[1].trim(),
        headline: "",
        primaryText: "",
        description: "",
        ctaButton: "",
      };
      continue;
    }
    if (!currentVariant) continue;

    if (/^\s*\*?\*?Headline\*?\*?\s*:/i.test(ln)) {
      currentVariant.headline = _stripBoldLabel(ln, "Headline");
    } else if (/^\s*\*?\*?Primary Text\*?\*?\s*:/i.test(ln)) {
      currentVariant.primaryText = _stripBoldLabel(ln, "Primary Text");
    } else if (/^\s*\*?\*?Description\*?\*?\s*:/i.test(ln)) {
      currentVariant.description = _stripBoldLabel(ln, "Description");
    } else if (/^\s*\*?\*?CTA Button\*?\*?\s*:/i.test(ln) || /^\s*\*?\*?CTA\*?\*?\s*:/i.test(ln)) {
      currentVariant.ctaButton = _stripBoldLabel(ln, "CTA Button").replace(/^CTA:\s*/i, "");
    } else if (currentVariant.primaryText && ln.trim() && !ln.trim().startsWith("**")) {
      // continuation of primary text body if no other label hit
      currentVariant.primaryText += " " + ln.trim();
    }
  }
  flushVariant();

  // Setup Guide — numbered list under "## Step-by-Step Setup Guide"
  const setupBlock = _extractSection(lines, /^##\s+Step-by-Step Setup Guide\b/i);
  const setupSteps = setupBlock
    .filter((ln) => /^\s*\d+\./.test(ln))
    .map((ln) => ln.replace(/^\s*\d+\.\s*/, "").trim())
    .filter(Boolean);

  return { campaignTitle, overview, audience, variants, setupSteps };
}

/* ─── Inline copy button ─── */

function CopyButton({
  text,
  fieldKey,
  copiedKey,
  setCopiedKey,
  onCopied,
  size = "sm",
}: {
  text: string;
  fieldKey: string;
  copiedKey: string | null;
  setCopiedKey: React.Dispatch<React.SetStateAction<string | null>>;
  onCopied?: (k: string) => void;
  size?: "sm" | "md";
}) {
  const isCopied = copiedKey === fieldKey;

  const handleCopy = async () => {
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopiedKey(fieldKey);
      onCopied?.(fieldKey);
      setTimeout(() => {
        setCopiedKey((current) => (current === fieldKey ? null : current));
      }, 1800);
    } catch (e) {
      console.error("Clipboard write failed", e);
    }
  };

  const padCls = size === "md" ? "px-3 py-1.5 text-xs" : "px-2 py-1 text-[11px]";

  return (
    <button
      onClick={handleCopy}
      disabled={!text}
      className={`inline-flex items-center gap-1 ${padCls} font-medium rounded-md border transition disabled:opacity-40 ${
        isCopied
          ? "bg-[#534AB7] text-white border-[#534AB7]"
          : "bg-white text-[#534AB7] border-[#E0DED8] hover:border-[#534AB7]/40 hover:bg-[#F8F8F6]"
      }`}
    >
      {isCopied ? (
        <>
          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
          </svg>
          Copied
        </>
      ) : (
        <>
          <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 17.25v3.375c0 .621-.504 1.125-1.125 1.125h-9.75a1.125 1.125 0 01-1.125-1.125V7.875c0-.621.504-1.125 1.125-1.125H6.75a9.06 9.06 0 011.5.124m7.5 10.376h3.375c.621 0 1.125-.504 1.125-1.125V11.25c0-4.46-3.243-8.161-7.5-8.876a9.06 9.06 0 00-1.5-.124H9.375c-.621 0-1.125.504-1.125 1.125v3.5m7.5 10.375H9.375a1.125 1.125 0 01-1.125-1.125v-9.25m12 6.625v-1.875a3.375 3.375 0 00-3.375-3.375h-1.5a1.125 1.125 0 01-1.125-1.125v-1.5a3.375 3.375 0 00-3.375-3.375H9.75" />
          </svg>
          Copy
        </>
      )}
    </button>
  );
}

/* ─── Main component ─── */

export default function CampaignCopyPasteTab({ tenantId, campaign, onCampaignUpdate }: Props) {
  const inboxItemId: string | undefined = campaign?.inbox_item_id;

  const [item, setItem] = useState<any>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [copiedKey, setCopiedKey] = useState<string | null>(null);
  const [pastedVariants, setPastedVariants] = useState<Set<string>>(new Set());
  const [launching, setLaunching] = useState(false);
  const [launchedAt, setLaunchedAt] = useState<string | null>(null);
  const [reviewAt, setReviewAt] = useState<string | null>(null);

  // Hydrate "pasted" state from campaign.metadata so the badge survives
  // a refresh. Backend's update_campaign shallow-merges these keys, so
  // they persist across browsers without a localStorage cache.
  useEffect(() => {
    if (!campaign?.id) return;
    const meta = campaign?.metadata || {};
    setLaunchedAt(meta.pasted_at || null);
    setReviewAt(meta.performance_review_at || null);
  }, [campaign?.id, campaign?.metadata]);

  // Lazy-fetch the inbox item this tab is bound to.
  useEffect(() => {
    let cancelled = false;
    if (!inboxItemId) {
      setItem(null);
      setLoading(false);
      return;
    }
    (async () => {
      setLoading(true);
      setError("");
      try {
        const res = await inboxApi.getItem(inboxItemId);
        if (cancelled) return;
        if (res?.item) setItem(res.item);
        else setError(res?.error || "Inbox item not found");
      } catch (e: any) {
        if (!cancelled) setError(e?.message || "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, [inboxItemId]);

  const plan = useMemo<ParsedPlan | null>(() => {
    const content: string = item?.content || "";
    if (!content) return null;
    return parsePlan(content);
  }, [item?.content]);

  const audienceBlockText = useMemo(() => (plan?.audience || []).join("\n"), [plan]);

  const totalVariants = plan?.variants.length || 0;

  const onVariantCopied = useCallback((fieldKey: string) => {
    // fieldKey looks like "v0:headline" — extract the variant index
    const variantPart = fieldKey.split(":")[0];
    if (!variantPart.startsWith("v")) return;
    setPastedVariants((prev) => {
      if (prev.has(variantPart)) return prev;
      const next = new Set(prev);
      next.add(variantPart);
      return next;
    });
  }, []);

  const handleLaunch = useCallback(async () => {
    if (!campaign?.id || launching) return;
    setLaunching(true);
    const now = new Date();
    const reviewDate = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000);
    const pastedAt = now.toISOString();
    const reviewIso = reviewDate.toISOString();

    try {
      // Single PATCH carries the status flip + the metadata stamps.
      // Backend shallow-merges metadata so other keys (e.g. campaign
      // objective parsed from the agent markdown) are preserved.
      await campaignsApi.update(tenantId, campaign.id, {
        status: "active",
        metadata: {
          pasted_at: pastedAt,
          performance_review_at: reviewIso,
        },
      });
      setLaunchedAt(pastedAt);
      setReviewAt(reviewIso);
    } catch (e) {
      console.error("Failed to mark campaign active:", e);
      // No localStorage fallback — single source of truth lives in
      // campaigns.metadata. User can retry the click on failure.
    }
    setLaunching(false);
    onCampaignUpdate();
  }, [campaign?.id, tenantId, launching, onCampaignUpdate]);

  /* ─── Empty / loading states ─── */

  if (!inboxItemId) {
    return (
      <div className="bg-white rounded-xl border border-[#E0DED8] p-8 text-center">
        <p className="text-sm text-[#5F5E5A]">
          This campaign has no linked Ad Strategist plan.
        </p>
        <p className="text-xs text-[#9E9C95] mt-1">
          Copy-paste content appears for campaigns created from an inbox draft.
        </p>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="flex justify-center py-12">
        <div className="w-6 h-6 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }

  if (error || !item || !plan) {
    return (
      <div className="bg-white rounded-xl border border-[#E0DED8] p-8 text-center">
        <p className="text-sm text-[#5F5E5A]">{error || "Could not parse campaign plan."}</p>
      </div>
    );
  }

  /* ─── Render ─── */

  return (
    <div className="space-y-5">
      {/* Header strip — title + paste-progress chip + review badge */}
      <div className="bg-white rounded-xl border border-[#E0DED8] p-5">
        <div className="flex flex-col sm:flex-row sm:items-start sm:justify-between gap-3">
          <div className="min-w-0 flex-1">
            <p className="text-[10px] uppercase tracking-wide text-[#9E9C95] font-semibold">Campaign Blueprint</p>
            <h2 className="text-base font-bold text-[#2C2C2A] mt-0.5 truncate">
              {plan.campaignTitle || campaign.campaign_name}
            </h2>
            <p className="text-xs text-[#5F5E5A] mt-1">
              Paste each field below into Meta Ads Manager. Tap Copy on a value to grab it cleanly.
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            {totalVariants > 0 && (
              <span className="px-2.5 py-1 text-[11px] font-semibold rounded-full bg-[#EEEDFE] text-[#534AB7]">
                {pastedVariants.size} of {totalVariants} variants pasted
              </span>
            )}
            {launchedAt && (
              <span className="px-2.5 py-1 text-[11px] font-semibold rounded-full bg-green-50 text-green-700">
                Pasted {new Date(launchedAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })}
              </span>
            )}
          </div>
        </div>
        {reviewAt && (
          <div className="mt-3 flex items-start gap-2 px-3 py-2 rounded-lg bg-blue-50 border border-blue-100">
            <svg className="w-4 h-4 text-blue-500 shrink-0 mt-0.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v6h4.5m4.5 0a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <p className="text-xs text-blue-800">
              Performance Review scheduled for{" "}
              <strong>
                {new Date(reviewAt).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
              </strong>{" "}
              — ARIA will check in on this campaign.
            </p>
          </div>
        )}
      </div>

      {/* CAMPAIGN — Overview block */}
      {plan.overview.length > 0 && (
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5 space-y-3">
          <div className="flex items-center gap-2">
            <span className="px-2 py-0.5 text-[10px] font-bold rounded bg-[#534AB7] text-white uppercase">Campaign</span>
            <h3 className="text-sm font-semibold text-[#2C2C2A]">Overview</h3>
          </div>
          <ul className="space-y-1.5">
            {plan.overview.map((ln, i) => (
              <li key={i} className="text-sm text-[#2C2C2A] leading-relaxed">
                {/* Render "Label: value" where Label is bolded inline */}
                {(() => {
                  const m = ln.match(/^\*?\*?(.+?)\*?\*?\s*:\s*(.+)$/);
                  if (m) {
                    return (
                      <>
                        <span className="font-semibold text-[#5F5E5A]">{m[1]}:</span>{" "}
                        <span>{m[2]}</span>
                      </>
                    );
                  }
                  return ln;
                })()}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* AD SET — Audience */}
      {plan.audience.length > 0 && (
        <div className="bg-white rounded-xl border border-[#E0DED8] p-5 space-y-3">
          <div className="flex items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="px-2 py-0.5 text-[10px] font-bold rounded bg-[#1D9E75] text-white uppercase">Ad Set</span>
              <h3 className="text-sm font-semibold text-[#2C2C2A]">Target Audience</h3>
            </div>
            <CopyButton
              text={audienceBlockText}
              fieldKey="audience"
              copiedKey={copiedKey}
              setCopiedKey={setCopiedKey}
            />
          </div>
          <ul className="space-y-1.5">
            {plan.audience.map((ln, i) => (
              <li key={i} className="text-sm text-[#2C2C2A] leading-relaxed pl-3 border-l-2 border-[#E0DED8]">
                {ln}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* ADS — Variants */}
      {plan.variants.length > 0 && (
        <div className="space-y-3">
          {plan.variants.map((v, idx) => {
            const variantKey = `v${idx}`;
            const isPasted = pastedVariants.has(variantKey);
            return (
              <div
                key={idx}
                className={`bg-white rounded-xl border p-5 space-y-3 ${
                  isPasted ? "border-[#534AB7]/30 ring-1 ring-[#534AB7]/10" : "border-[#E0DED8]"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="px-2 py-0.5 text-[10px] font-bold rounded bg-[#D85A30] text-white uppercase">Ad</span>
                    <h3 className="text-sm font-semibold text-[#2C2C2A]">{v.label}</h3>
                  </div>
                  {isPasted && (
                    <span className="text-[10px] font-medium text-[#534AB7] flex items-center gap-1">
                      <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                      </svg>
                      Copied
                    </span>
                  )}
                </div>

                <CopyableField
                  label="Headline"
                  value={v.headline}
                  fieldKey={`${variantKey}:headline`}
                  copiedKey={copiedKey}
                  setCopiedKey={setCopiedKey}
                  onCopied={onVariantCopied}
                />
                <CopyableField
                  label="Primary Text"
                  value={v.primaryText}
                  fieldKey={`${variantKey}:primary`}
                  copiedKey={copiedKey}
                  setCopiedKey={setCopiedKey}
                  onCopied={onVariantCopied}
                  multiline
                />
                <CopyableField
                  label="Description"
                  value={v.description}
                  fieldKey={`${variantKey}:description`}
                  copiedKey={copiedKey}
                  setCopiedKey={setCopiedKey}
                  onCopied={onVariantCopied}
                />
                <CopyableField
                  label="CTA Button"
                  value={v.ctaButton}
                  fieldKey={`${variantKey}:cta`}
                  copiedKey={copiedKey}
                  setCopiedKey={setCopiedKey}
                  onCopied={onVariantCopied}
                />
              </div>
            );
          })}
        </div>
      )}

      {/* Setup Guide — display only, no copy buttons */}
      {plan.setupSteps.length > 0 && (
        <div className="bg-[#F8F8F6] rounded-xl border border-[#E0DED8] p-5">
          <h3 className="text-sm font-semibold text-[#2C2C2A] mb-3">Step-by-Step Setup Guide</h3>
          <ol className="space-y-2">
            {plan.setupSteps.map((step, i) => (
              <li key={i} className="text-sm text-[#2C2C2A] leading-relaxed flex gap-3">
                <span className="shrink-0 w-6 h-6 rounded-full bg-[#534AB7] text-white text-[11px] font-bold flex items-center justify-center">
                  {i + 1}
                </span>
                <span className="pt-0.5">{step}</span>
              </li>
            ))}
          </ol>
        </div>
      )}

      {/* Launch action */}
      <div className="pt-2">
        {launchedAt ? (
          <div className="bg-white rounded-xl border border-green-200 bg-green-50 p-4 flex items-center gap-3">
            <div className="w-9 h-9 rounded-full bg-green-100 flex items-center justify-center shrink-0">
              <svg className="w-5 h-5 text-green-600" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
              </svg>
            </div>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-green-800">
                Pasted on {new Date(launchedAt).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })}
              </p>
              <p className="text-xs text-green-700 mt-0.5">
                Performance Review {reviewAt
                  ? `scheduled for ${new Date(reviewAt).toLocaleDateString("en-US", { month: "short", day: "numeric" })}`
                  : "will run in 7 days"}.
              </p>
            </div>
          </div>
        ) : (
          <button
            onClick={handleLaunch}
            disabled={launching}
            className="w-full py-3 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition disabled:opacity-50 flex items-center justify-center gap-2"
          >
            {launching ? (
              <>
                <div className="w-4 h-4 border-2 border-white border-t-transparent rounded-full animate-spin" />
                Marking active...
              </>
            ) : (
              <>
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                </svg>
                I have pasted this into Meta Ads Manager
              </>
            )}
          </button>
        )}
      </div>
    </div>
  );
}

/* ─── Sub: copyable field row ─── */

function CopyableField({
  label,
  value,
  fieldKey,
  copiedKey,
  setCopiedKey,
  onCopied,
  multiline,
}: {
  label: string;
  value: string;
  fieldKey: string;
  copiedKey: string | null;
  setCopiedKey: React.Dispatch<React.SetStateAction<string | null>>;
  onCopied?: (k: string) => void;
  multiline?: boolean;
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <p className="text-[10px] uppercase tracking-wide text-[#9E9C95] font-semibold">{label}</p>
        <CopyButton
          text={value}
          fieldKey={fieldKey}
          copiedKey={copiedKey}
          setCopiedKey={setCopiedKey}
          onCopied={onCopied}
        />
      </div>
      {value ? (
        <div
          className={`text-sm text-[#2C2C2A] bg-[#F8F8F6] border border-[#E0DED8] rounded-lg px-3 py-2 leading-relaxed ${
            multiline ? "whitespace-pre-wrap" : ""
          }`}
        >
          {value}
        </div>
      ) : (
        <p className="text-xs text-[#9E9C95] italic">Not provided.</p>
      )}
    </div>
  );
}
