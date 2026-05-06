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
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
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
    if (password !== confirmPassword) {
      setGeneralError("Passwords do not match.");
      return;
    }
    setEmailLoading(true);
    const trimmedEmail = email.trim().toLowerCase();
    const { data, error } = await supabase.auth.signUp({
      email: trimmedEmail,
      password,
      options: {
        // Stash full_name in user_metadata so the profiles backfill +
        // the admin user table can show a human-readable name later.
        // Supabase exposes this on the JWT as user_metadata.full_name.
        data: fullName.trim() ? { full_name: fullName.trim() } : undefined,
        emailRedirectTo: `${window.location.origin}/auth/confirm`,
      },
    });
    // Path 1: explicit error. Supabase returns "User already registered"
    // when the project has email-confirmation disabled. Route them back
    // to the login page with a contextual error param the login page
    // surfaces verbatim — instead of hiding them on signup with a
    // banner.
    if (error) {
      const m = error.message.toLowerCase();
      if (m.includes("already registered") || m.includes("already exists") || m.includes("user already")) {
        router.replace("/login?error=already_registered");
        return;
      }
      setGeneralError(error.message);
      setEmailLoading(false);
      return;
    }
    // Path 2: silent collision. When email-confirmation IS enabled (the
    // Supabase default), signUp returns 200 + a stub user with an empty
    // `identities` array as a privacy-preserving "user enumeration"
    // mitigation — the attacker can't tell from the response whether
    // the address is registered. We explicitly want the legitimate
    // user to know, so detect the empty-identities shape and route to
    // login.
    if (data?.user && Array.isArray(data.user.identities) && data.user.identities.length === 0) {
      router.replace("/login?error=already_registered");
      return;
    }
    // Path 3: real success. If a session came back the project has
    // email-confirmation off — go straight to onboarding. Otherwise
    // route to the dedicated /auth/check-email page.
    if (data?.session) {
      router.replace("/welcome");
      return;
    }
    router.replace(`/check-email?email=${encodeURIComponent(trimmedEmail)}`);
  }

  async function handleGoogleSignUp() {
    setGeneralError("");
    setGoogleLoading(true);
    // Sign-up only requests Google's basic scopes (no Gmail). Same
    // reasoning as the login page — keeps the OAuth flow off the
    // sensitive-scopes gate so any user can sign up without being on a
    // test-users allowlist. Gmail integration is opt-in via the
    // Settings → Integrations flow.
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/auth/callback?mode=signup`,
        queryParams: {
          prompt: "select_account",
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
          <img src="/logo.png" alt="ARIA" className="h-14 w-14 rounded-full object-cover shadow-lg shadow-[#534AB7]/20" />
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
            <div className="relative">
              <input
                id="password"
                type={showPassword ? "text" : "password"}
                autoComplete="new-password"
                required
                minLength={8}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                disabled={anyLoading}
                placeholder="••••••••"
                className="w-full h-12 px-3 pr-12 rounded-lg border border-[#E0DED8] bg-white text-[15px] text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7] disabled:opacity-60"
              />
              <button
                type="button"
                onClick={() => setShowPassword((v) => !v)}
                disabled={anyLoading}
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
            {password.length > 0 && password.length < 8 && (
              <p className="text-[11px] text-[#D85A30] mt-1">Password must be at least 8 characters.</p>
            )}
          </div>
          <div>
            <label htmlFor="confirm_password" className="block text-xs font-medium text-[#5F5E5A] mb-1">
              Confirm password
            </label>
            <input
              id="confirm_password"
              type={showPassword ? "text" : "password"}
              autoComplete="new-password"
              required
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              disabled={anyLoading}
              placeholder="••••••••"
              className="w-full h-12 px-3 rounded-lg border border-[#E0DED8] bg-white text-[15px] text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7] disabled:opacity-60"
            />
            {confirmPassword.length > 0 && confirmPassword !== password && (
              <p className="text-[11px] text-[#D85A30] mt-1">Passwords do not match.</p>
            )}
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

        {/* Google OAuth — secondary, smaller. Kept for users still on
            Google's Test-mode allowlist; primary signup is now email +
            password. */}
        <button
          type="button"
          onClick={handleGoogleSignUp}
          disabled={anyLoading}
          className="w-full flex items-center justify-center gap-2 border border-[#E0DED8] rounded-lg h-10 text-xs font-medium text-[#5F5E5A] hover:bg-[#F8F8F6] transition disabled:opacity-60 disabled:cursor-not-allowed"
        >
          {googleLoading ? (
            <div className="w-4 h-4 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
          ) : (
            <span className="scale-[0.8] inline-flex"><GoogleIcon /></span>
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
