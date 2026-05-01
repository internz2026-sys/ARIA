"use client";

/**
 * CampaignStatusSelector — colored status pill that lets the user move
 * a campaign through its state machine. Wraps StatusDropdown so the
 * pill matches the Projects Kanban styling.
 *
 * Important: setting `status='active'` is reserved for the
 * "I have pasted" flow in CampaignCopyPasteTab — that path also
 * stamps `metadata.pasted_at` and `metadata.performance_review_at`.
 * Letting users flip back to "active" via this dropdown would skip
 * the stamps, so the "Active" option is hidden from the menu unless
 * the campaign is already active (in which case it appears as the
 * current selection but can't be re-selected).
 *
 * Coder 1's whitelist on the backend enforces the value set
 * (draft/active/paused/completed/archived); anything else 400's, so
 * the same allow-list is mirrored here for the menu options.
 */

import React, { useState } from "react";
import StatusDropdown, { StatusOption } from "./StatusDropdown";
import { campaigns as campaignsApi } from "@/lib/api";
import { useNotifications } from "@/lib/use-notifications";

const ALL_OPTIONS: StatusOption[] = [
  { key: "draft", label: "Draft", color: "#5F5E5A", bg: "#F0EFEC" },
  { key: "active", label: "Active", color: "#1D9E75", bg: "#E6F5EE" },
  { key: "paused", label: "Paused", color: "#A87C00", bg: "#FFF7E0" },
  { key: "completed", label: "Completed", color: "#2563EB", bg: "#E0ECFE" },
  { key: "archived", label: "Archived", color: "#7C3AED", bg: "#EFE7FE" },
];

interface Props {
  tenantId: string;
  campaignId: string;
  current: string;
  onChanged: () => void;
}

export default function CampaignStatusSelector({ tenantId, campaignId, current, onChanged }: Props) {
  const { showToast } = useNotifications();
  const [saving, setSaving] = useState(false);

  // Hide "active" from the menu unless that's already the current
  // value — the "I have pasted" button is the only path to active.
  const options: StatusOption[] = ALL_OPTIONS.filter(
    (o) => o.key !== "active" || current === "active",
  );

  const handleChange = async (next: string) => {
    if (next === current) return;
    if (next === "active") {
      // Defensive: should never happen because we hide the option,
      // but if a stale state somehow lets it through, refuse with a
      // helpful toast pointing the user to the Copy-Paste tab.
      showToast({
        title: "Use the Copy-Paste tab",
        body: "Mark a campaign active by clicking 'I have pasted' on the Copy-Paste tab.",
        variant: "warning",
      });
      return;
    }
    setSaving(true);
    try {
      await campaignsApi.updateStatus(tenantId, campaignId, next);
      showToast({ title: `Status updated to ${next}`, variant: "success" });
      onChanged();
    } catch (e: any) {
      showToast({
        title: "Failed to update status",
        body: e?.message || "",
        variant: "error",
      });
    } finally {
      setSaving(false);
    }
  };

  return (
    <StatusDropdown
      value={current}
      options={options}
      onChange={handleChange}
      disabled={saving}
    />
  );
}
