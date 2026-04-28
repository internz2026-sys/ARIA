"use client";

import React, { useState } from "react";
import { supabase } from "@/lib/supabase";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [error, setError] = useState("");
  const [sent, setSent] = useState(false);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!email.trim()) {
      setError("Email is required");
      return;
    }
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      setError("Enter a valid email address");
      return;
    }
    setError("");
    setLoading(true);
    const { error: resetError } = await supabase.auth.resetPasswordForEmail(email.trim(), {
      redirectTo: `${window.location.origin}/auth/callback?next=/login`,
    });
    setLoading(false);
    if (resetError) {
      setError(resetError.message);
      return;
    }
    setSent(true);
  }

  return (
    <div className="w-full max-w-[420px] mx-auto px-6 py-10">
      <div className="bg-white rounded-2xl border border-[#E0DED8] shadow-sm p-8">
        {!sent ? (
          <>
            {/* Icon */}
            <div className="flex justify-center mb-5">
              <div className="w-12 h-12 rounded-full bg-[#EEEDFE] flex items-center justify-center">
                <svg width="22" height="22" fill="none" viewBox="0 0 24 24">
                  <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" stroke="#534AB7" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                  <polyline points="22,6 12,13 2,6" stroke="#534AB7" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </div>
            </div>

            <h1 className="text-[24px] font-bold text-[#2C2C2A] text-center mb-1">Reset your password</h1>
            <p className="text-[#5F5E5A] text-center mb-7 text-[15px]">
              Enter your email and we&apos;ll send you a reset link
            </p>

            <form onSubmit={handleSubmit} className="space-y-4">
              <div>
                <label className="block text-sm font-medium text-[#2C2C2A] mb-1.5">Email address</label>
                <input
                  type="email"
                  value={email}
                  onChange={(e) => { setEmail(e.target.value); setError(""); }}
                  placeholder="jane@company.com"
                  className={`w-full h-11 rounded-lg border px-3.5 text-sm text-[#2C2C2A] placeholder:text-[#B0AFA8] outline-none transition focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7] ${error ? "border-[#D85A30]" : "border-[#E0DED8]"}`}
                />
                {error && <p className="text-xs text-[#D85A30] mt-1">{error}</p>}
              </div>

              <button
                type="submit"
                disabled={loading}
                className="w-full h-11 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#433AA0] transition shadow-sm disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {loading ? "Sending..." : "Send reset link"}
              </button>
            </form>
          </>
        ) : (
          /* Success state */
          <>
            <div className="flex justify-center mb-5">
              <div className="w-12 h-12 rounded-full bg-[#E6F7F0] flex items-center justify-center">
                <svg width="22" height="22" fill="none" viewBox="0 0 24 24">
                  <path d="M20 6L9 17l-5-5" stroke="#1D9E75" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </div>
            </div>

            <h1 className="text-[24px] font-bold text-[#2C2C2A] text-center mb-2">Check your email</h1>
            <p className="text-[#5F5E5A] text-center text-[15px] mb-6">
              We sent a password reset link to<br />
              <span className="font-medium text-[#2C2C2A]">{email}</span>
            </p>

            <p className="text-xs text-[#5F5E5A] text-center mb-6">
              Didn&apos;t receive it? Check your spam folder or{" "}
              <button
                onClick={() => setSent(false)}
                className="text-[#534AB7] font-medium hover:underline"
              >
                try again
              </button>
            </p>
          </>
        )}

        {/* Back to login */}
        <a
          href="/login"
          className="flex items-center justify-center gap-1.5 text-sm text-[#534AB7] font-medium hover:underline mt-2"
        >
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          Back to sign in
        </a>
      </div>
    </div>
  );
}
