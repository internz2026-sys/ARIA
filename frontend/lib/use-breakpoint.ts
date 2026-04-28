"use client";

import { useEffect, useState } from "react";

/**
 * Single source of truth for viewport-size checks across the app. Wraps
 * `window.matchMedia` with proper SSR safety, listener cleanup, and a
 * named breakpoint vocabulary that mirrors Tailwind's defaults so the
 * JS branches stay aligned with the CSS ones.
 *
 * Why this exists: ad-hoc `window.matchMedia(...)` and `window.innerWidth
 * < N` checks scattered across components drift apart over time — one
 * file says `768`, another says `1024`, a third says `<= 767`. When a
 * designer asks "what counts as mobile?" there's no single answer. This
 * hook makes the answer one word: `useBreakpoint("md")` returns true at
 * the same width as Tailwind's `md:` prefix activates. Move the line
 * once in Tailwind config (or here) and everything follows.
 *
 * Tailwind defaults (px):
 *   sm: 640   md: 768   lg: 1024   xl: 1280   2xl: 1536
 *
 * Returns true when the viewport is AT LEAST the named breakpoint —
 * matches Tailwind's mobile-first semantics where `md:flex` means "flex
 * at md and above". Use the negation if you want "below md".
 */
export const BREAKPOINTS = {
  sm: 640,
  md: 768,
  lg: 1024,
  xl: 1280,
  "2xl": 1536,
} as const;

export type Breakpoint = keyof typeof BREAKPOINTS;

/** True when the viewport is at or above the named breakpoint. */
export function useBreakpoint(name: Breakpoint): boolean {
  const [matches, setMatches] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(`(min-width: ${BREAKPOINTS[name]}px)`).matches;
  });

  useEffect(() => {
    if (typeof window === "undefined") return;
    const mql = window.matchMedia(`(min-width: ${BREAKPOINTS[name]}px)`);
    const update = () => setMatches(mql.matches);
    update();
    // `addEventListener("change", ...)` is the modern API; the older
    // `addListener` is deprecated but Safari <14 still needs it. We use
    // the modern API only since the rest of the codebase already
    // assumes evergreen browsers.
    mql.addEventListener("change", update);
    return () => mql.removeEventListener("change", update);
  }, [name]);

  return matches;
}

/** True when the viewport is BELOW the named breakpoint — the common
 *  "is this mobile?" check. `useBelowBreakpoint("md")` returns true on
 *  phones (< 768px), matching where Tailwind's `md:` prefix turns ON. */
export function useBelowBreakpoint(name: Breakpoint): boolean {
  return !useBreakpoint(name);
}

/** Returns the largest matching breakpoint name, or `null` for sub-sm
 *  viewports (typical phones in portrait). Useful when a component
 *  wants to fan out into more than two cases (sm vs md vs lg). */
export function useActiveBreakpoint(): Breakpoint | null {
  const sm = useBreakpoint("sm");
  const md = useBreakpoint("md");
  const lg = useBreakpoint("lg");
  const xl = useBreakpoint("xl");
  const xxl = useBreakpoint("2xl");
  if (xxl) return "2xl";
  if (xl) return "xl";
  if (lg) return "lg";
  if (md) return "md";
  if (sm) return "sm";
  return null;
}
