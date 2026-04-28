"use client";

import React, { useState, useEffect } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";

function GoogleIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 48 48">
      <path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/>
      <path fill="#4285F4" d="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>
      <path fill="#FBBC05" d="M10.53 28.59a14.5 14.5 0 0 1 0-9.18l-7.98-6.19a24.0 24.0 0 0 0 0 21.56l7.98-6.19z"/>
      <path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/>
    </svg>
  );
}

export default function SignUpPage() {
  const router = useRouter();
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [generalError, setGeneralError] = useState("");
  const [verifyMessage, setVerifyMessage] = useState("");
  const [emailLoading, setEmailLoading] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);

  // Only auto-redirect if fully onboarded (session + tenant_id)
  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (session && localStorage.getItem("aria_tenant_id")) {
        router.replace("/dashboard");
      }
    });
  }, [router]);

  async function handleEmailSignUp(e: React.FormEvent) {
    e.preventDefault();
    setGeneralError("");
    setVerifyMessage("");
    if (!email.trim() || !password) {
      setGeneralError("Email and password are required.");
      return;
    }
    if (password.length < 8) {
      setGeneralError("Password must be at least 8 characters.");
      return;
    }
    setEmailLoading(true);
    const { data, error } = await supabase.auth.signUp({
      email: email.trim().toLowerCase(),
      password,
      options: {
        // Stash full_name in user_metadata so the profiles backfill +
        // the admin user table can show a human-readable name later.
        // Supabase exposes this on the JWT as user_metadata.full_name.
        data: fullName.trim() ? { full_name: fullName.trim() } : undefined,
        emailRedirectTo: `${window.location.origin}/auth/callback?mode=signup`,
      },
    });
    if (error) {
      setGeneralError(error.message);
      setEmailLoading(false);
      return;
    }
    // Supabase returns a session immediately when email confirmation is
    // disabled in project settings. When confirmation IS required, the
    // session is null and we need to tell the user to check their inbox.
    if (data?.session) {
      router.replace("/welcome");
      return;
    }
    setVerifyMessage(
      "Check your email — we sent you a confirmation link. Click it to finish creating your account.",
    );
    setEmailLoading(false);
  }

  async function handleGoogleSignUp() {
    setGeneralError("");
    setGoogleLoading(true);
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/auth/callback?mode=signup`,
        scopes: "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly",
        queryParams: {
          access_type: "offline",
          prompt: "consent",
        },
      },
    });
    if (error) {
      setGeneralError(error.message);
      setGoogleLoading(false);
    }
  }

  const anyLoading = emailLoading || googleLoading;

  return (
    <div className="w-full max-w-[420px] mx-auto px-6 py-10">
      <div className="bg-white rounded-2xl border border-[#E0DED8] shadow-sm p-6 sm:p-8">
        {/* Logo */}
        <div className="flex justify-center mb-6">
          <img src="/logo.webp" alt="ARIA" className="h-14 w-14 rounded-full object-cover shadow-lg shadow-[#534AB7]/20" />
        </div>

        {/* Badge */}
        <div className="flex justify-center mb-5">
          <span className="inline-flex items-center gap-1.5 rounded-full bg-[#EEEDFE] px-3 py-1 text-xs font-medium text-[#534AB7]">
            14-day free trial &mdash; no credit card
          </span>
        </div>

        <h1 className="text-[26px] font-bold text-[#2C2C2A] text-center mb-1">Create your account</h1>
        <p className="text-[#5F5E5A] text-center mb-6 text-[15px]">Start automating your marketing with AI agents</p>

        {/* General error */}
        {generalError && (
          <div className="mb-5 p-3 rounded-lg bg-[#FEF2EE] border border-[#F5C6B3] text-[#D85A30] text-sm">
            {generalError}
          </div>
        )}
        {verifyMessage && (
          <div className="mb-5 p-3 rounded-lg bg-[#E6F5ED] border border-[#1D9E75]/30 text-[#1D9E75] text-sm">
            {verifyMessage}
          </div>
        )}

        {/* Email + password form */}
        <form onSubmit={handleEmailSignUp} className="space-y-3">
          <div>
            <label htmlFor="full_name" className="block text-xs font-medium text-[#5F5E5A] mb-1">Full name</label>
            <input
              id="full_name"
              type="text"
              autoComplete="name"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              disabled={anyLoading}
              placeholder="Jane Doe"
              className="w-full h-12 px-3 rounded-lg border border-[#E0DED8] bg-white text-[15px] text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7] disabled:opacity-60"
            />
          </div>
          <div>
            <label htmlFor="email" className="block text-xs font-medium text-[#5F5E5A] mb-1">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={anyLoading}
              placeholder="you@example.com"
              className="w-full h-12 px-3 rounded-lg border border-[#E0DED8] bg-white text-[15px] text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7] disabled:opacity-60"
            />
          </div>
          <div>
            <label htmlFor="password" className="block text-xs font-medium text-[#5F5E5A] mb-1">
              Password <span className="text-[#9E9C95] font-normal">(min 8 chars)</span>
            </label>
            <input
              id="password"
              type="password"
              autoComplete="new-password"
              required
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={anyLoading}
              placeholder="••••••••"
              className="w-full h-12 px-3 rounded-lg border border-[#E0DED8] bg-white text-[15px] text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7] disabled:opacity-60"
            />
          </div>
          <button
            type="submit"
            disabled={anyLoading}
            className="w-full flex items-center justify-center gap-2 h-12 rounded-lg bg-[#534AB7] text-white text-sm font-semibold hover:bg-[#433AA0] transition disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {emailLoading ? (
              <div className="w-5 h-5 border-2 border-white/40 border-t-white rounded-full animate-spin" />
            ) : null}
            {emailLoading ? "Creating account..." : "Get started"}
          </button>
        </form>

        {/* Divider */}
        <div className="flex items-center gap-3 my-5">
          <div className="flex-1 h-px bg-[#E0DED8]" />
          <span className="text-[11px] uppercase tracking-wide text-[#9E9C95]">or</span>
          <div className="flex-1 h-px bg-[#E0DED8]" />
        </div>

        {/* Google OAuth */}
        <button
          type="button"
          onClick={handleGoogleSignUp}
          disabled={anyLoading}
          className="w-full flex items-center justify-center gap-2.5 border border-[#E0DED8] rounded-lg h-12 text-sm font-medium text-[#2C2C2A] hover:bg-[#F8F8F6] transition disabled:opacity-60 disabled:cursor-not-allowed"
        >
          {googleLoading ? (
            <div className="w-5 h-5 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
          ) : (
            <GoogleIcon />
          )}
          {googleLoading ? "Redirecting..." : "Continue with Google"}
        </button>

        <p className="text-xs text-[#B0AFA8] text-center mt-5 leading-relaxed">
          By creating an account you agree to our{" "}
          <a href="/terms" className="underline hover:text-[#534AB7]">Terms of Service</a> and{" "}
          <a href="/privacy" className="underline hover:text-[#534AB7]">Privacy Policy</a>.
        </p>
      </div>

      {/* Sign in link */}
      <p className="text-sm text-[#5F5E5A] text-center mt-6">
        Already have an account?{" "}
        <a href="/login" className="text-[#534AB7] font-semibold hover:underline">Sign in</a>
      </p>
    </div>
  );
}
