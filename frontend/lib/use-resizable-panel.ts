"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type ResizeCorner = "nw" | "ne" | "sw" | "se";

/**
 * Give a floating panel a visible, cursor-styled resize handle on ONE corner
 * and persist the chosen size to localStorage. Which corner depends on the
 * button position (caller passes it in) — we always offer resize from the
 * corner farthest from the button so the drag direction feels natural
 * (dragging away from the button grows the panel).
 *
 * Why custom handles instead of CSS `resize: both`:
 * - The native bottom-right grip is nearly invisible and users don't
 *   discover it.
 * - With our button-anchored position math, native resize fights the
 *   layout (dragging toward the button shrinks the panel *away* from the
 *   drag direction because position recomputes from the button).
 *
 * Usage:
 *   const { size, startResize, cursorClass } = useResizablePanel(
 *     "aria-ceo-chat-size", { w: 420, h: 520 }, "nw",
 *   );
 *   <div style={{ width: size.w, height: size.h }}>
 *     <div onMouseDown={startResize} className={cursorClass + " ..."}>grip</div>
 *   </div>
 */
export function useResizablePanel(
  storageKey: string,
  defaults: { w: number; h: number },
  corner: ResizeCorner,
  constraints: { minW?: number; minH?: number } = {},
) {
  const [size, setSize] = useState(defaults);
  const sizeRef = useRef(size);
  sizeRef.current = size;

  // Restore persisted size once per key.
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
    (e: React.MouseEvent) => {
      e.preventDefault();
      e.stopPropagation();
      const startX = e.clientX;
      const startY = e.clientY;
      const startW = sizeRef.current.w;
      const startH = sizeRef.current.h;
      const minW = constraints.minW ?? 320;
      const minH = constraints.minH ?? 320;

      function onMove(ev: MouseEvent) {
        const dx = ev.clientX - startX;
        const dy = ev.clientY - startY;
        // A corner of "nw" means the handle is on top-left → dragging
        // further top-left should grow the panel, so use -dx/-dy.
        let newW = startW;
        let newH = startH;
        newW = corner === "nw" || corner === "sw" ? startW - dx : startW + dx;
        newH = corner === "nw" || corner === "ne" ? startH - dy : startH + dy;

        const maxW = typeof window !== "undefined" ? window.innerWidth - 40 : 1200;
        const maxH = typeof window !== "undefined" ? window.innerHeight - 40 : 800;
        newW = Math.max(minW, Math.min(maxW, newW));
        newH = Math.max(minH, Math.min(maxH, newH));

        setSize({ w: newW, h: newH });
      }
      function onUp() {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        try {
          localStorage.setItem(storageKey, JSON.stringify(sizeRef.current));
        } catch {
          /* ignore */
        }
      }
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    },
    [corner, storageKey, constraints.minW, constraints.minH],
  );

  // `cursorClass` matches the visual direction of the corner. Tailwind's
  // `cursor-nwse-resize` is the ↖↘ arrow; `cursor-nesw-resize` is ↗↙.
  const cursorClass =
    corner === "nw" || corner === "se" ? "cursor-nwse-resize" : "cursor-nesw-resize";

  return { size, startResize, cursorClass, corner };
}
