import { useCallback, useEffect, useRef, useState } from "react";

interface DragState {
  dragging: boolean;
  moved: boolean;
  startX: number;
  startY: number;
  elX: number;
  elY: number;
}

/**
 * Makes a fixed-position element draggable with zero-lag GPU-accelerated movement.
 * Uses transform: translate3d() instead of left/top for compositor-level performance.
 * Panel followers get position updates via RAF-throttled state sync during drag.
 *
 * Returns:
 *  - pos: current {x,y} (synced during drag via RAF + on release)
 *  - btnRef: attach to the draggable element
 *  - handleMouseDown: attach to onMouseDown (desktop mouse)
 *  - handleTouchStart: attach to onTouchStart (mobile / tablet touch)
 *  - handleClick: attach to onClick (filters out drags)
 *  - dragging: true during active drag
 */
export function useDraggable(initialX: number, initialY: number, storageKey?: string) {
  const [pos, setPos] = useState({ x: -1, y: -1 });
  const posRef = useRef({ x: -1, y: -1 });
  const dragRef = useRef<DragState>({ dragging: false, moved: false, startX: 0, startY: 0, elX: 0, elY: 0 });
  const btnRef = useRef<HTMLButtonElement>(null);
  const onDragStartRef = useRef<(() => void) | null>(null);
  const rafRef = useRef<number | null>(null);

  const clampToViewport = useCallback((p: { x: number; y: number }) => {
    const isNarrow = window.innerWidth < 768;
    const bottomSafe = isNarrow ? 96 : 56;
    const maxX = Math.max(0, window.innerWidth - 180);
    const maxY = Math.max(0, window.innerHeight - bottomSafe);
    return {
      x: Math.max(8, Math.min(maxX, p.x)),
      y: Math.max(8, Math.min(maxY, p.y)),
    };
  }, []);

  // Init position on mount — restore from localStorage if available,
  // then clamp to the current viewport. Previously the initial position
  // was used verbatim, which meant a caller that passed
  // `window.innerWidth - 200` for the x anchor could land the button
  // off-screen on narrow phones (<400px) or on rotation. Clamping here
  // guarantees the widget is always reachable without a drag first.
  useEffect(() => {
    let p = { x: initialX, y: initialY };
    if (storageKey) {
      try {
        const saved = localStorage.getItem(`aria_widget_pos_${storageKey}`);
        if (saved) {
          const parsed = JSON.parse(saved);
          if (parsed.x >= 0 && parsed.x < window.innerWidth - 60 && parsed.y >= 0 && parsed.y < window.innerHeight - 40) {
            p = parsed;
          }
        }
      } catch {}
    }

    // Clamp to viewport with a bigger bottom safe-area on narrow
    // (mobile) viewports so the button doesn't cover the OS keyboard
    // or a system nav bar when it eventually opens. The margin on
    // mobile matches Tailwind `bottom-20` (~5rem) — same visual
    // budget the prompt calls out.
    p = clampToViewport(p);

    setPos(p);
    posRef.current = p;
    if (btnRef.current) {
      btnRef.current.style.transform = `translate3d(${p.x}px, ${p.y}px, 0)`;
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Re-clamp on viewport changes (orientation flip, browser zoom,
  // window resize). Without this, a widget pinned to bottom-right on
  // landscape ends up half-off-screen after rotating to portrait.
  useEffect(() => {
    const onResize = () => {
      const next = clampToViewport(posRef.current);
      if (next.x === posRef.current.x && next.y === posRef.current.y) return;
      posRef.current = next;
      setPos(next);
      if (btnRef.current) {
        btnRef.current.style.transform = `translate3d(${next.x}px, ${next.y}px, 0)`;
      }
      if (storageKey) {
        try { localStorage.setItem(`aria_widget_pos_${storageKey}`, JSON.stringify(next)); } catch {}
      }
    };
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      window.removeEventListener("orientationchange", onResize);
    };
  }, [clampToViewport, storageKey]);

  // Shared drag step — applies the per-frame translate + clamp + RAF
  // throttle. Used by both the mouse and touch handlers.
  const stepDrag = useCallback((clientX: number, clientY: number) => {
    const d = dragRef.current;
    const btn = btnRef.current;
    const dx = clientX - d.startX;
    const dy = clientY - d.startY;
    if (!d.moved && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
      d.moved = true;
      onDragStartRef.current?.();
    }
    if (d.moved && btn) {
      const next = clampToViewport({ x: d.elX + dx, y: d.elY + dy });
      btn.style.transform = `translate3d(${next.x}px, ${next.y}px, 0)`;
      posRef.current = next;
      if (rafRef.current === null) {
        rafRef.current = requestAnimationFrame(() => {
          setPos({ ...posRef.current });
          rafRef.current = null;
        });
      }
    }
  }, [clampToViewport]);

  const finishDrag = useCallback(() => {
    const d = dragRef.current;
    d.dragging = false;
    if (rafRef.current !== null) {
      cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    setPos({ ...posRef.current });
    if (storageKey && d.moved) {
      try { localStorage.setItem(`aria_widget_pos_${storageKey}`, JSON.stringify(posRef.current)); } catch {}
    }
  }, [storageKey]);

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const d = dragRef.current;
    d.dragging = true;
    d.moved = false;
    d.startX = e.clientX;
    d.startY = e.clientY;
    d.elX = posRef.current.x;
    d.elY = posRef.current.y;

    function onMove(ev: MouseEvent) {
      stepDrag(ev.clientX, ev.clientY);
    }
    function onUp() {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      finishDrag();
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [stepDrag, finishDrag]);

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    if (!e.touches.length) return;
    const t0 = e.touches[0];
    const d = dragRef.current;
    d.dragging = true;
    d.moved = false;
    d.startX = t0.clientX;
    d.startY = t0.clientY;
    d.elX = posRef.current.x;
    d.elY = posRef.current.y;

    // passive:false so we can preventDefault and stop the page from
    // scrolling under the finger while dragging the widget. Without
    // this, the user's drag gesture also scrolls the inbox list,
    // which is jarring.
    function onTouchMove(ev: TouchEvent) {
      if (!ev.touches.length) return;
      if (d.moved) ev.preventDefault();
      const t = ev.touches[0];
      stepDrag(t.clientX, t.clientY);
    }
    function onTouchEnd() {
      document.removeEventListener("touchmove", onTouchMove);
      document.removeEventListener("touchend", onTouchEnd);
      document.removeEventListener("touchcancel", onTouchEnd);
      finishDrag();
    }
    document.addEventListener("touchmove", onTouchMove, { passive: false });
    document.addEventListener("touchend", onTouchEnd);
    document.addEventListener("touchcancel", onTouchEnd);
  }, [stepDrag, finishDrag]);

  const handleClick = useCallback(() => {
    // Only fire if it wasn't a drag
    return !dragRef.current.moved;
  }, []);

  return {
    pos,
    posRef,
    btnRef,
    handleMouseDown,
    handleTouchStart,
    handleClick,
    /** Register a callback that fires when drag starts (e.g. close panel) */
    onDragStart: (fn: () => void) => { onDragStartRef.current = fn; },
    dragging: dragRef.current.dragging,
  };
}
