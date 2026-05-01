"use client";

import { useSyncExternalStore } from "react";

// Tiny module-level coordination store so unrelated dashboard widgets
// can react to "an overlay panel is open" without a Context provider
// being threaded through layout.tsx. Used today to hide the CEO Chat
// FAB while the Notifications panel is open (the two were stacking on
// top of each other in the bottom-right corner). Add new flags here
// as new full-height side panels appear.

interface OverlayState {
  notificationsOpen: boolean;
}

let state: OverlayState = { notificationsOpen: false };
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
}

function getSnapshot(): OverlayState {
  return state;
}

// SSR fallback — render an empty overlay state on the server so the
// client mount doesn't bail with a hydration mismatch.
function getServerSnapshot(): OverlayState {
  return { notificationsOpen: false };
}

export function setNotificationsOpen(open: boolean) {
  if (state.notificationsOpen === open) return;
  state = { ...state, notificationsOpen: open };
  emit();
}

export function useUiOverlay(): OverlayState {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
