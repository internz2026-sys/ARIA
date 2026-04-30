"use client";

import { useCallback, useState } from "react";

// Master-Detail view toggle for mobile-first responsive pages.
//
// Inbox + Conversations both use the same pattern: on mobile, the list
// pane shows or the detail pane shows — never both — and a Back button
// pops the detail back to the list. On desktop both panes are visible
// simultaneously, so the toggle becomes a no-op.
//
// The Tailwind class strings stay in each component's JSX because the
// JIT needs full strings (template literals with variables defeat the
// scanner). What this hook centralizes is just the state shape +
// semantic helpers, so future pages adopt the same pattern by importing
// instead of re-implementing.
//
// Usage:
//   const { mobileShowDetail, showDetail, hideDetail } = useViewToggle();
//   // tap a row -> showDetail(); back button -> hideDetail();
//   <div className={mobileShowDetail ? "hidden" : "flex"} ...>list</div>
//   <div className={mobileShowDetail ? "flex" : "hidden"} ...>detail</div>
//
// `setMobileShowDetail` is also returned for edge cases where a callsite
// needs to set the value conditionally (e.g. error paths that should
// not jump to detail mode).

export interface ViewToggle {
  /** Whether the detail pane should show on mobile (>= @3xl always shows both). */
  mobileShowDetail: boolean;
  /** Switch to the detail pane on mobile. Call after setSelected(item). */
  showDetail: () => void;
  /** Pop back to the list on mobile. Call from the Back button. */
  hideDetail: () => void;
  /** Raw setter — escape hatch for callsites that need conditional control. */
  setMobileShowDetail: (value: boolean) => void;
}

export function useViewToggle(initial: boolean = false): ViewToggle {
  const [mobileShowDetail, setMobileShowDetail] = useState(initial);
  const showDetail = useCallback(() => setMobileShowDetail(true), []);
  const hideDetail = useCallback(() => setMobileShowDetail(false), []);
  return { mobileShowDetail, setMobileShowDetail, showDetail, hideDetail };
}
