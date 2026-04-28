"use client";

import { useEffect, useState } from "react";

/**
 * Detects whether the on-screen keyboard is open on a mobile browser
 * by watching `window.visualViewport`. When the keyboard slides up,
 * `visualViewport.height` shrinks below `window.innerHeight` by
 * roughly the keyboard height. Returns `{ open, height }` where
 * `height` is the keyboard's pixel height (0 when closed).
 *
 * Falls back to `{ open: false, height: 0 }` on browsers without
 * VisualViewport (older Android, IE) and on the server, so callers
 * can safely use it without conditional rendering.
 *
 * Why VisualViewport vs `resize`: a plain `window.resize` listener
 * fires when the URL bar collapses too, which we DON'T want to treat
 * as a keyboard event. VisualViewport distinguishes the two — only
 * fires `resize` when the visible viewport actually shrinks under the
 * window viewport.
 */
export function useKeyboardState() {
  const [state, setState] = useState({ open: false, height: 0 });

  useEffect(() => {
    if (typeof window === "undefined" || !window.visualViewport) return;
    const vv = window.visualViewport;

    const update = () => {
      // 60px threshold absorbs the iOS Safari URL-bar collapse (~50px)
      // and prevents flicker when the user scrolls. Real keyboards on
      // Android phones are 240-320px so this gates cleanly.
      const delta = window.innerHeight - vv.height;
      const open = delta > 60;
      setState((prev) =>
        prev.open === open && Math.abs(prev.height - delta) < 4
          ? prev
          : { open, height: open ? Math.round(delta) : 0 },
      );
    };

    update();
    vv.addEventListener("resize", update);
    vv.addEventListener("scroll", update);
    return () => {
      vv.removeEventListener("resize", update);
      vv.removeEventListener("scroll", update);
    };
  }, []);

  return state;
}
