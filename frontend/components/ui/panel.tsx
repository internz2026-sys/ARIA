import React from "react";
import { cn } from "@/lib/utils";

// Dashboard surface primitive — the single source of truth for the
// `bg-white rounded-xl border border-[#E0DED8]` chrome that ARIA's
// dashboard pages repeat ~100 times. Keeps cards, list panels, kpi
// tiles, and modal-like content blocks consistent so a future tweak
// to padding / shadow / corner radius lands everywhere at once.
//
// `Panel` is intentionally separate from the existing
// `components/ui/card.tsx` (which is used by marketing pages and
// includes shadow + token-based borders). Dashboard cards are flat,
// so a separate primitive avoids forcing every dashboard surface to
// "opt out" of marketing-card defaults.
//
// Variants:
//   tone="default"  -> standard white surface with E0DED8 border
//   tone="muted"    -> F8F8F6 background (e.g. inset section blocks)
//   tone="warning"  -> FFFAEC + D4B24C/40 border (Stagnation Monitor)
//   tone="danger"   -> FDF3EE + D85A30/40 border (suspended/over-cap)
//
// Usage:
//   <Panel className="p-4">...</Panel>
//   <Panel tone="warning" className="p-3">stale items</Panel>
//   <Panel as="section" className="p-6">grouped settings</Panel>

type PanelTone = "default" | "muted" | "warning" | "danger";

const TONE_CLASSES: Record<PanelTone, string> = {
  default: "bg-white border-[#E0DED8]",
  muted: "bg-[#F8F8F6] border-[#E0DED8]",
  warning: "bg-[#FFFAEC] border-[#D4B24C]/40",
  danger: "bg-[#FDF3EE] border-[#D85A30]/40",
};

interface PanelProps extends React.HTMLAttributes<HTMLDivElement> {
  tone?: PanelTone;
  /** Render as a different element (section/article/aside) for semantics. */
  as?: "div" | "section" | "article" | "aside";
}

export const Panel = React.forwardRef<HTMLDivElement, PanelProps>(
  ({ className, tone = "default", as: Tag = "div", ...props }, ref) => {
    return (
      <Tag
        ref={ref as any}
        className={cn(
          "rounded-xl border",
          TONE_CLASSES[tone],
          className,
        )}
        {...props}
      />
    );
  },
);
Panel.displayName = "Panel";
