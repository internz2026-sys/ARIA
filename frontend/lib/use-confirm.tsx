"use client";

import React, { createContext, useCallback, useContext, useState } from "react";

interface ConfirmOptions {
  title: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  destructive?: boolean;
}

interface ConfirmContextValue {
  /** Show a confirmation modal and resolve to true if user confirms. */
  confirm: (opts: ConfirmOptions) => Promise<boolean>;
}

const ConfirmContext = createContext<ConfirmContextValue>({
  confirm: async () => false,
});

export function useConfirm() {
  return useContext(ConfirmContext);
}

interface PendingConfirm {
  opts: ConfirmOptions;
  resolve: (ok: boolean) => void;
}

/**
 * Provider that owns a single confirm modal at the dashboard layout
 * level. Any descendant can call `useConfirm().confirm({...})` and
 * await a boolean. Replaces the native browser `confirm()` calls,
 * which look broken next to the rest of the design system and can't
 * be styled for dark mode.
 *
 * Usage:
 *   const { confirm } = useConfirm();
 *   const ok = await confirm({
 *     title: "Delete this contact?",
 *     message: "This cannot be undone.",
 *     destructive: true,
 *   });
 *   if (ok) doIt();
 */
export function ConfirmProvider({ children }: { children: React.ReactNode }) {
  const [pending, setPending] = useState<PendingConfirm | null>(null);

  const confirm = useCallback((opts: ConfirmOptions): Promise<boolean> => {
    return new Promise((resolve) => {
      setPending({ opts, resolve });
    });
  }, []);

  const handleResult = (ok: boolean) => {
    if (pending) {
      pending.resolve(ok);
      setPending(null);
    }
  };

  return (
    <ConfirmContext.Provider value={{ confirm }}>
      {children}
      {pending && (
        <div
          className="fixed inset-0 z-[80] flex items-center justify-center bg-black/40 backdrop-blur-sm"
          onClick={() => handleResult(false)}
        >
          <div
            className="bg-white rounded-xl border border-[#E0DED8] shadow-2xl max-w-md w-full mx-4 overflow-hidden"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="px-6 pt-6 pb-2">
              <div className="flex items-center gap-3">
                {pending.opts.destructive ? (
                  <div className="w-10 h-10 rounded-full bg-red-50 flex items-center justify-center flex-shrink-0">
                    <svg className="w-5 h-5 text-red-500" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                    </svg>
                  </div>
                ) : (
                  <div className="w-10 h-10 rounded-full bg-[#EEEDFE] flex items-center justify-center flex-shrink-0">
                    <svg className="w-5 h-5 text-[#534AB7]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                    </svg>
                  </div>
                )}
                <h3 className="text-lg font-semibold text-[#2C2C2A]">{pending.opts.title}</h3>
              </div>
            </div>
            {pending.opts.message && (
              <div className="px-6 py-4">
                <p className="text-sm text-[#5F5E5A] leading-relaxed">{pending.opts.message}</p>
              </div>
            )}
            <div className="px-6 pb-6 pt-2 flex items-center justify-end gap-3">
              <button
                onClick={() => handleResult(false)}
                className="px-4 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors"
              >
                {pending.opts.cancelLabel || "Cancel"}
              </button>
              <button
                onClick={() => handleResult(true)}
                className={`px-4 py-2 text-sm font-medium rounded-lg text-white transition-colors ${
                  pending.opts.destructive
                    ? "bg-red-500 hover:bg-red-600"
                    : "bg-[#534AB7] hover:bg-[#433AA0]"
                }`}
                autoFocus
              >
                {pending.opts.confirmLabel || "Confirm"}
              </button>
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}
