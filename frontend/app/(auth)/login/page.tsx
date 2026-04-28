"use client";

import React, { useState, useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
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

export default function LoginPage() {
  return (
    <Suspense fallback={<div className="w-full max-w-[420px] mx-auto px-6 py-10"><div className="h-96 flex items-center justify-center"><div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" /></div></div>}>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [generalError, setGeneralError] = useState("");
  const [emailLoading, setEmailLoading] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);

  useEffect(() => {
    const error = searchParams.get("error");
    if (error === "auth_failed") {
      setGeneralError("Authentication failed. Please try again.");
    } else if (error === "no_account") {
      setGeneralError("No account found with this Google account. Please sign up first.");
    }
  }, [searchParams]);

  // Only auto-redirect if fully onboarded (session + tenant_id)
  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (session && localStorage.getItem("aria_tenant_id")) {
        router.replace("/dashboard");
      }
    });
  }, [router]);

  async function handleEmailSignIn(e: React.FormEvent) {
    e.preventDefault();
    setGeneralError("");
    if (!email.trim() || !password) {
      setGeneralError("Email and password are required.");
      return;
    }
    setEmailLoading(true);
    const { data, error } = await supabase.auth.signInWithPassword({
      email: email.trim().toLowerCase(),
      password,
    });
    if (error) {
      setGeneralError(error.message);
      setEmailLoading(false);
      return;
    }
    // Same post-auth routing as the dashboard layout: tenant_id present
    // means onboarding is complete -> /dashboard; otherwise the user
    // needs to finish onboarding at /welcome. Look up the tenant by
    // owner email so a returning user who clears localStorage still
    // ends up in the right place.
    const userEmail = data?.user?.email;
    let nextRoute = "/welcome";
    if (userEmail) {
      try {
        const apiBase = process.env.NEXT_PUBLIC_API_URL || "";
        const res = await fetch(
          `${apiBase}/api/tenant/by-email/${encodeURIComponent(userEmail)}`,
          { headers: { Authorization: `Bearer ${data.session?.access_token || ""}` } },
        );
        const json = await res.json();
        if (json.tenant_id) {
          localStorage.setItem("aria_tenant_id", json.tenant_id);
          nextRoute = "/dashboard";
        }
      } catch {
        // Backend hiccup — fall through to /welcome and let the
        // dashboard layout double-check on next mount.
      }
    }
    router.replace(nextRoute);
  }

  async function handleGoogleSignIn() {
    setGeneralError("");
    setGoogleLoading(true);
    // Sign-in only requests Google's basic profile + email scopes (no
    // Gmail). That way the OAuth client doesn't trip Google's app
    // verification gate for sensitive scopes — anyone can sign up
    // without being on a test-users allowlist. Gmail integration runs
    // through its own OAuth flow on the Settings → Integrations page,
    // so the Gmail scopes are only requested when the user explicitly
    // opts into the Gmail feature.
    //
    // `prompt=select_account` forces Google's account picker every
    // time, so the browser's last-used Google account doesn't get
    // auto-selected.
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/auth/callback?mode=login`,
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
          <img src="/logo.webp" alt="ARIA" className="h-14 w-14 rounded-full object-cover shadow-lg shadow-[#534AB7]/20" />
        </div>

        <h1 className="text-[26px] font-bold text-[#2C2C2A] text-center mb-1">Welcome back</h1>
        <p className="text-[#5F5E5A] text-center mb-6 text-[15px]">Sign in to your ARIA account</p>

        {/* General error */}
        {generalError && (
          <div className="mb-5 p-3 rounded-lg bg-[#FEF2EE] border border-[#F5C6B3] text-[#D85A30] text-sm">
            {generalError}
          </div>
        )}

        {/* Email + password form */}
        <form onSubmit={handleEmailSignIn} className="space-y-3">
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
            <div className="flex items-center justify-between mb-1">
              <label htmlFor="password" className="text-xs font-medium text-[#5F5E5A]">Password</label>
              <a href="/forgot-password" className="text-xs text-[#534AB7] hover:underline">
                Forgot password?
              </a>
            </div>
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              required
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
            {emailLoading ? "Signing in..." : "Sign in"}
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
          onClick={handleGoogleSignIn}
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
      </div>

      {/* Sign up link */}
      <p className="text-sm text-[#5F5E5A] text-center mt-6">
        Don&apos;t have an account?{" "}
        <a href="/signup" className="text-[#534AB7] font-semibold hover:underline">Get started</a>
      </p>
    </div>
  );
}
