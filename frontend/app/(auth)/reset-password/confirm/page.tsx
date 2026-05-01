"use client";

import React, { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";

export default function ResetPasswordConfirmPage() {
  const router = useRouter();
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [generalError, setGeneralError] = useState("");
  const [loading, setLoading] = useState(false);
  const [hasRecoverySession, setHasRecoverySession] = useState<boolean | null>(null);

  // Supabase parses the recovery token out of the URL fragment as soon
  // as the supabase-js client mounts, then fires onAuthStateChange with
  // event === "PASSWORD_RECOVERY". Until that fires we show a checking-
  // state; if no session ever shows up, the link was invalid/expired.
  useEffect(() => {
    let cancelled = false;

    // Initial check: if the user already has a session (the client
    // hydrated the recovery token from the fragment before this effect
    // ran), proceed.
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (cancelled) return;
      if (session) setHasRecoverySession(true);
    });

    const { data: sub } = supabase.auth.onAuthStateChange((event) => {
      if (cancelled) return;
      if (event === "PASSWORD_RECOVERY" || event === "SIGNED_IN") {
        setHasRecoverySession(true);
      }
    });

    // If after 1.5s nothing has hydrated, mark the link as invalid.
    const t = setTimeout(() => {
      if (cancelled) return;
      setHasRecoverySession((prev) => (prev === null ? false : prev));
    }, 1500);

    return () => {
      cancelled = true;
      clearTimeout(t);
      sub.subscription.unsubscribe();
    };
  }, []);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setGeneralError("");
    if (password.length < 8) {
      setGeneralError("Password must be at least 8 characters.");
      return;
    }
    if (password !== confirmPassword) {
      setGeneralError("Passwords do not match.");
      return;
    }
    setLoading(true);
    const { error } = await supabase.auth.updateUser({ password });
    setLoading(false);
    if (error) {
      setGeneralError(error.message);
      return;
    }
    // Surface a success cue via sessionStorage so the dashboard can
    // pop a toast on next mount (no router state available across a
    // hard navigation).
    try {
      sessionStorage.setItem("aria_password_reset_success", "1");
    } catch {}
    router.replace("/dashboard");
  }

  return (
    <div className="w-full max-w-[420px] mx-auto px-6 py-10">
      <div className="bg-white rounded-2xl border border-[#E0DED8] shadow-sm p-6 sm:p-8">
        {hasRecoverySession === null && (
          <div className="text-center py-10">
            <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin mx-auto" />
            <p className="text-sm text-[#5F5E5A] mt-3">Verifying reset link...</p>
          </div>
        )}

        {hasRecoverySession === false && (
          <>
            <div className="flex justify-center mb-5">
              <div className="w-12 h-12 rounded-full bg-[#FEF2EE] flex items-center justify-center">
                <svg width="22" height="22" fill="none" viewBox="0 0 24 24">
                  <path d="M12 9v4M12 17h.01M5.07 19h13.86c1.54 0 2.5-1.67 1.73-3L13.73 4a2 2 0 00-3.46 0L3.34 16c-.77 1.33.19 3 1.73 3z" stroke="#D85A30" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </div>
            </div>
            <h1 className="text-[22px] font-bold text-[#2C2C2A] text-center mb-1">Invalid or expired link</h1>
            <p className="text-[#5F5E5A] text-center text-[14px] mb-6">
              This password reset link has expired or has already been used. Request a new one.
            </p>
            <a
              href="/reset-password"
              className="block w-full h-11 leading-[2.75rem] text-center rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#433AA0] transition"
            >
              Request a new link
            </a>
          </>
        )}

        {hasRecoverySession === true && (
          <>
            <div className="flex justify-center mb-5">
              <div className="w-12 h-12 rounded-full bg-[#EEEDFE] flex items-center justify-center">
                <svg width="22" height="22" fill="none" viewBox="0 0 24 24">
                  <rect x="5" y="11" width="14" height="9" rx="2" stroke="#534AB7" strokeWidth="1.5" />
                  <path d="M8 11V8a4 4 0 118 0v3" stroke="#534AB7" strokeWidth="1.5" />
                </svg>
              </div>
            </div>

            <h1 className="text-[24px] font-bold text-[#2C2C2A] text-center mb-1">Set a new password</h1>
            <p className="text-[#5F5E5A] text-center mb-6 text-[15px]">
              Choose a new password for your ARIA account.
            </p>

            {generalError && (
              <div className="mb-4 p-3 rounded-lg bg-[#FEF2EE] border border-[#F5C6B3] text-[#D85A30] text-sm">
                {generalError}
              </div>
            )}

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label htmlFor="password" className="block text-xs font-medium text-[#5F5E5A] mb-1">
                  New password <span className="text-[#9E9C95] font-normal">(min 8 chars)</span>
                </label>
                <div className="relative">
                  <input
                    id="password"
                    type={showPassword ? "text" : "password"}
                    autoComplete="new-password"
                    required
                    minLength={8}
                    value={password}
                    onChange={(e) => setPassword(e.target.value)}
                    disabled={loading}
                    placeholder="••••••••"
                    className="w-full h-12 px-3 pr-12 rounded-lg border border-[#E0DED8] bg-white text-[15px] text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7] disabled:opacity-60"
                  />
                  <button
                    type="button"
                    onClick={() => setShowPassword((v) => !v)}
                    disabled={loading}
                    aria-label={showPassword ? "Hide password" : "Show password"}
                    className="absolute inset-y-0 right-0 flex items-center px-3 text-[#9E9C95] hover:text-[#534AB7] transition disabled:opacity-60"
                  >
                    {showPassword ? (
                      <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.6">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M3 3l18 18M10.5 10.5a2 2 0 002.83 2.83M9.88 5.09A10.94 10.94 0 0112 5c5 0 9.27 3.11 11 7-.46 1.04-1.13 2-1.97 2.84M6.61 6.61C4.6 7.96 3.06 9.84 2 12c1.73 3.89 6 7 11 7 1.49 0 2.92-.27 4.24-.76" />
                      </svg>
                    ) : (
                      <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.6">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
                        <circle cx="12" cy="12" r="3" />
                      </svg>
                    )}
                  </button>
                </div>
              </div>

              <div>
                <label htmlFor="confirm_password" className="block text-xs font-medium text-[#5F5E5A] mb-1">Confirm new password</label>
                <input
                  id="confirm_password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="new-password"
                  required
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  disabled={loading}
                  placeholder="••••••••"
                  className="w-full h-12 px-3 rounded-lg border border-[#E0DED8] bg-white text-[15px] text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7] disabled:opacity-60"
                />
                {confirmPassword.length > 0 && confirmPassword !== password && (
                  <p className="text-[11px] text-[#D85A30] mt-1">Passwords do not match.</p>
                )}
              </div>

              <button
                type="submit"
                disabled={loading || password.length < 8 || password !== confirmPassword}
                className="w-full h-12 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#433AA0] transition disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {loading ? "Updating..." : "Update password"}
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}
