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
 *  - handleMouseDown: attach to onMouseDown
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
    const isNarrow = window.innerWidth < 768;
    const maxX = Math.max(0, window.innerWidth - 180);
    const bottomSafe = isNarrow ? 96 : 56;
    const maxY = Math.max(0, window.innerHeight - bottomSafe);
    p = {
      x: Math.max(8, Math.min(maxX, p.x)),
      y: Math.max(8, Math.min(maxY, p.y)),
    };

    setPos(p);
    posRef.current = p;
    if (btnRef.current) {
      btnRef.current.style.transform = `translate3d(${p.x}px, ${p.y}px, 0)`;
    }
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  const handleMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const d = dragRef.current;
    d.dragging = true;
    d.moved = false;
    d.startX = e.clientX;
    d.startY = e.clientY;
    d.elX = posRef.current.x;
    d.elY = posRef.current.y;
    const btn = btnRef.current;

    function onMove(ev: MouseEvent) {
      const dx = ev.clientX - d.startX;
      const dy = ev.clientY - d.startY;
      if (!d.moved && (Math.abs(dx) > 4 || Math.abs(dy) > 4)) {
        d.moved = true;
        onDragStartRef.current?.();
      }
      if (d.moved && btn) {
        // Keep the same bottom safe-area policy as the mount-time
        // clamp: mobile needs more breathing room below so the
        // button doesn't end up under the keyboard / bottom nav.
        const isNarrow = window.innerWidth < 768;
        const bottomSafe = isNarrow ? 96 : 56;
        const nx = Math.max(0, Math.min(window.innerWidth - 180, d.elX + dx));
        const ny = Math.max(0, Math.min(window.innerHeight - bottomSafe, d.elY + dy));
        btn.style.transform = `translate3d(${nx}px, ${ny}px, 0)`;
        posRef.current = { x: nx, y: ny };
        // Sync React state so panels follow (RAF-throttled)
        if (rafRef.current === null) {
          rafRef.current = requestAnimationFrame(() => {
            setPos({ ...posRef.current });
            rafRef.current = null;
          });
        }
      }
    }

    function onUp() {
      d.dragging = false;
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      setPos({ ...posRef.current });
      // Persist position to localStorage
      if (storageKey && d.moved) {
        try { localStorage.setItem(`aria_widget_pos_${storageKey}`, JSON.stringify(posRef.current)); } catch {}
      }
    }

    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, []);

  const handleClick = useCallback(() => {
    // Only fire if it wasn't a drag
    return !dragRef.current.moved;
  }, []);

  return {
    pos,
    posRef,
    btnRef,
    handleMouseDown,
    handleClick,
    /** Register a callback that fires when drag starts (e.g. close panel) */
    onDragStart: (fn: () => void) => { onDragStartRef.current = fn; },
    dragging: dragRef.current.dragging,
  };
}
