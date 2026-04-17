"use client";

import { useEffect, useRef, useState } from "react";

/**
 * Give a floating panel user-controlled resize via the native CSS
 * `resize: both` corner grip, and persist the chosen size to localStorage
 * so it survives page navigation and reloads.
 *
 * Usage:
 *   const { panelRef, size } = useResizablePanel("aria-chat-panel", { w: 420, h: 520 });
 *   <div ref={panelRef} style={{ width: size.w, height: size.h, resize: "both", overflow: "hidden" }}>
 *
 * The hook reads the persisted size once on mount, then watches the element
 * with a ResizeObserver so every user drag updates both the React state
 * (positioning math can read it) and localStorage (debounced to avoid
 * spamming writes while the user is still dragging).
 */
export function useResizablePanel(
  storageKey: string,
  defaults: { w: number; h: number },
) {
  const [size, setSize] = useState(defaults);
  const panelRef = useRef<HTMLDivElement>(null);

  // One-time restore from localStorage.
  useEffect(() => {
    try {
      const raw = localStorage.getItem(storageKey);
      if (!raw) return;
      const s = JSON.parse(raw);
      if (typeof s?.w === "number" && typeof s?.h === "number") {
        setSize({ w: s.w, h: s.h });
      }
    } catch {
      /* ignore */
    }
  }, [storageKey]);

  // Sync size from the DOM (when the user drags the resize grip) back to
  // state + localStorage. Debounced so rapid resize events don't hammer
  // localStorage or trigger a flood of re-renders downstream.
  useEffect(() => {
    const el = panelRef.current;
    if (!el) return;
    let saveTimer: ReturnType<typeof setTimeout> | null = null;
    const ro = new ResizeObserver(() => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      setSize((prev) => (prev.w === w && prev.h === h ? prev : { w, h }));
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        try {
          localStorage.setItem(storageKey, JSON.stringify({ w, h }));
        } catch {
          /* ignore */
        }
      }, 400);
    });
    ro.observe(el);
    return () => {
      ro.disconnect();
      if (saveTimer) clearTimeout(saveTimer);
    };
  }, [storageKey]);

  return { panelRef, size };
}
