"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type ResizeHandle = "n" | "e" | "s" | "w" | "nw" | "ne" | "sw" | "se";
export type ResizeCorner = "nw" | "ne" | "sw" | "se";

/**
 * Give a floating panel resize handles on its two far edges AND its far
 * corner (relative to the anchor button), plus persist the chosen size
 * to localStorage. Handles on "near" sides are omitted because resizing
 * them would fight the button-anchored positioning math in the consumer.
 *
 * Perf:
 * Drag updates skip React reconciliation entirely — if you pass a
 * `panelRef` (and optional `computePosition`) in options the hook writes
 * width/height/left/top straight to the DOM element, rAF-batched to one
 * paint per frame. React state only catches up on mouseup. Without this,
 * a chat panel with many rendered messages re-renders its whole subtree
 * per pixel of drag, which is visibly laggy.
 *
 * Usage:
 *   const panelRef = useRef<HTMLDivElement>(null);
 *   const { size, startResize, handles, cursorClass, corner } =
 *     useResizablePanel(
 *       "aria-ceo-chat-size",
 *       { w: 420, h: 520 },
 *       "nw",
 *       { minW: 320, minH: 360 },
 *       { panelRef, computePosition: (s) => ({ left: ..., top: ... }) },
 *     );
 *   {handles.map(h => (
 *     <div key={h} onMouseDown={startResize(h)} className="..." />
 *   ))}
 */
export function useResizablePanel(
  storageKey: string,
  defaults: { w: number; h: number },
  corner: ResizeCorner,
  constraints: { minW?: number; minH?: number } = {},
  options: {
    panelRef?: React.RefObject<HTMLElement | null>;
    computePosition?: (size: { w: number; h: number }) => { left: number; top: number };
  } = {},
) {
  const [size, setSize] = useState(defaults);
  const sizeRef = useRef(size);
  sizeRef.current = size;

  // Keep a live ref to options so the mousemove handler reads the latest
  // computePosition closure without re-creating startResize each render.
  const optionsRef = useRef(options);
  optionsRef.current = options;

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

  const startResize = useCallback(
    (handle: ResizeHandle) => (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const startX = e.clientX;
      const startY = e.clientY;
      const startW = sizeRef.current.w;
      const startH = sizeRef.current.h;
      const minW = constraints.minW ?? 320;
      const minH = constraints.minH ?? 320;

      const touchesW = handle === "nw" || handle === "w" || handle === "sw";
      const touchesE = handle === "ne" || handle === "e" || handle === "se";
      const touchesN = handle === "nw" || handle === "n" || handle === "ne";
      const touchesS = handle === "sw" || handle === "s" || handle === "se";

      let lastW = startW;
      let lastH = startH;
      let rafId = 0;

      function onMove(ev: MouseEvent) {
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;
        let newW = startW;
        let newH = startH;
        if (touchesW) newW = startW - dx;
        else if (touchesE) newW = startW + dx;
        if (touchesN) newH = startH - dy;
        else if (touchesS) newH = startH + dy;

        const maxW = typeof window !== "undefined" ? window.innerWidth - 40 : 1200;
        const maxH = typeof window !== "undefined" ? window.innerHeight - 40 : 800;
        newW = Math.max(minW, Math.min(maxW, newW));
        newH = Math.max(minH, Math.min(maxH, newH));
        lastW = newW;
        lastH = newH;

        // Coalesce mousemove to one paint per frame. Writing to the DOM
        // directly keeps React out of the drag's hot path — otherwise
        // every pixel of drag re-renders the whole consumer subtree.
        if (rafId) return;
        rafId = requestAnimationFrame(() => {
          rafId = 0;
          const { panelRef, computePosition } = optionsRef.current;
          const el = panelRef?.current;
          if (el) {
            el.style.width = lastW + "px";
            el.style.height = lastH + "px";
            if (computePosition) {
              const pos = computePosition({ w: lastW, h: lastH });
              el.style.left = pos.left + "px";
              el.style.top = pos.top + "px";
            }
          } else {
            // Fallback for consumers that don't pass a ref — state-driven,
            // slower, but keeps the hook working.
            setSize({ w: lastW, h: lastH });
          }
        });
      }

      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        if (rafId) {
          cancelAnimationFrame(rafId);
          rafId = 0;
        }
        // Commit the final size to React so any sidecar logic (callbacks,
        // derived layout) observes it. For the direct-DOM path the panel
        // is already at the right size visually; this just syncs state.
        setSize({ w: lastW, h: lastH });
        try {
          localStorage.setItem(storageKey, JSON.stringify({ w: lastW, h: lastH }));
        } catch {
          /* ignore */
        }
      }

      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [storageKey, constraints.minW, constraints.minH],
  );

  // Two far edges + far corner, relative to the anchor button.
  const handles: ResizeHandle[] =
    corner === "nw" ? ["n", "w", "nw"] :
    corner === "ne" ? ["n", "e", "ne"] :
    corner === "sw" ? ["s", "w", "sw"] :
    ["s", "e", "se"];

  const cursorClass =
    corner === "nw" || corner === "se" ? "cursor-nwse-resize" : "cursor-nesw-resize";

  return { size, startResize, cursorClass, corner, handles };
}
