"use client";

import React, { useEffect, useRef, useState } from "react";

export interface StatusOption {
  key: string;
  label: string;
  color: string; // hex (border + text accent)
  bg: string;    // hex (pill background)
}

interface StatusDropdownProps {
  value: string;
  options: StatusOption[];
  onChange: (newValue: string) => void;
  /** Optional: disable the dropdown (e.g. while saving) */
  disabled?: boolean;
}

/**
 * Custom dropdown styled as a colored status pill, replacing the
 * native HTML <select> which renders an OS-default arrow that breaks
 * the pill aesthetic. Used in CRM tables for contact status / deal
 * stage / etc.
 *
 * Behavior:
 *   - Click pill -> popover opens with all options
 *   - Click outside -> closes
 *   - Escape -> closes
 *   - Arrow up/down -> navigate options when open
 *   - Enter -> select highlighted option
 *   - Click option -> selects + closes
 */
export default function StatusDropdown({ value, options, onChange, disabled }: StatusDropdownProps) {
  const [open, setOpen] = useState(false);
  const [highlightIdx, setHighlightIdx] = useState(0);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);

  const current = options.find((o) => o.key === value) || options[0];

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      const t = e.target as Node;
      if (triggerRef.current?.contains(t) || popoverRef.current?.contains(t)) return;
      setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  // Keyboard nav when open
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setHighlightIdx((i) => Math.min(options.length - 1, i + 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setHighlightIdx((i) => Math.max(0, i - 1));
      } else if (e.key === "Enter") {
        e.preventDefault();
        const opt = options[highlightIdx];
        if (opt) {
          onChange(opt.key);
          setOpen(false);
        }
      }
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, highlightIdx, options, onChange]);

  // Initialize highlight to current value when opening
  useEffect(() => {
    if (open) {
      const idx = options.findIndex((o) => o.key === value);
      setHighlightIdx(idx >= 0 ? idx : 0);
    }
  }, [open, options, value]);

  if (!current) return null;

  return (
    <div className="relative inline-block">
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        onClick={(e) => { e.stopPropagation(); setOpen((v) => !v); }}
        className="text-[11px] font-medium px-2.5 py-1 rounded-full border cursor-pointer focus:outline-none focus:ring-2 focus:ring-offset-1 disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1"
        style={{
          color: current.color,
          backgroundColor: current.bg,
          borderColor: current.color + "40",
        }}
      >
        <span>{current.label}</span>
        {/* Custom chevron, no native arrow */}
        <svg className="w-3 h-3 opacity-70" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <div
          ref={popoverRef}
          className="absolute z-50 mt-1 left-0 min-w-[140px] bg-white rounded-lg border border-[#E0DED8] shadow-lg overflow-hidden"
        >
          {options.map((opt, idx) => (
            <button
              key={opt.key}
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onChange(opt.key);
                setOpen(false);
              }}
              onMouseEnter={() => setHighlightIdx(idx)}
              className={`w-full flex items-center gap-2 px-3 py-2 text-xs text-left transition-colors ${
                idx === highlightIdx ? "bg-[#F8F8F6]" : ""
              } ${opt.key === value ? "font-semibold" : ""}`}
            >
              <span
                className="w-2 h-2 rounded-full shrink-0"
                style={{ backgroundColor: opt.color }}
              />
              <span style={{ color: idx === highlightIdx ? "#2C2C2A" : "#5F5E5A" }}>{opt.label}</span>
              {opt.key === value && (
                <svg className="w-3 h-3 ml-auto text-[#534AB7]" fill="none" stroke="currentColor" strokeWidth={3} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
