"use client";

import React, { useState, useEffect, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { supabase } from "@/lib/supabase";
import { authFetch } from "@/lib/api";

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
  const [showPassword, setShowPassword] = useState(false);
  const [generalError, setGeneralError] = useState("");
  const [emailLoading, setEmailLoading] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(false);

  useEffect(() => {
    const error = searchParams.get("error");
    if (error === "auth_failed") {
      setGeneralError("Authentication failed. Please try again.");
    } else if (error === "no_account") {
      setGeneralError("No account found with this Google account. Please sign up first.");
    } else if (error === "already_registered") {
      setGeneralError("This email is already registered. Sign in below.");
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
    const cleanEmail = email.trim().toLowerCase();
    setEmailLoading(true);

    // Check rate limit BEFORE submitting credentials. If the account is
    // locked out from too many recent failed attempts, refuse to call
    // Supabase at all -- saves a round-trip and gives the user a clear
    // "wait N minutes" message instead of a generic auth error.
    const apiBase = process.env.NEXT_PUBLIC_API_URL || "";
    try {
      const statusRes = await fetch(
        `${apiBase}/api/auth/login-status?email=${encodeURIComponent(cleanEmail)}`,
        { cache: "no-store" },
      );
      if (statusRes.ok) {
        const s = await statusRes.json();
        if (s && s.allowed === false) {
          const mins = Math.max(1, Math.ceil((s.retry_after_seconds || 60) / 60));
          setGeneralError(
            `Too many failed login attempts. Try again in ${mins} minute${mins > 1 ? "s" : ""}.`
          );
          setEmailLoading(false);
          return;
        }
      }
    } catch {
      // If the rate-limit API is down, don't block the user -- fail
      // open. Supabase still has its own coarse rate limit underneath.
    }

    const { data, error } = await supabase.auth.signInWithPassword({
      email: cleanEmail,
      password,
    });
    if (error) {
      // Banned-user check: Supabase intentionally MASKS the ban error
      // as "Invalid login credentials" to avoid being a user-existence
      // oracle, so we can't rely on `error.message` or `error.code` to
      // detect a ban. Instead, on ANY login error we ask our backend
      // whether the email belongs to a banned profile — if yes, route
      // to /banned. The endpoint returns `{banned: false}` for unknown
      // emails (same no-oracle property), so a wrong password on a
      // valid-but-unbanned account still falls through to the generic
      // error display.
      try {
        const banRes = await fetch(
          `${apiBase}/api/auth/ban-status-by-email/${encodeURIComponent(cleanEmail)}`,
          { cache: "no-store" },
        );
        if (banRes.ok) {
          const banData = await banRes.json();
          if (banData?.banned && banData?.user_id) {
            router.replace(`/banned?user=${encodeURIComponent(banData.user_id)}`);
            return;
          }
        }
      } catch {
        // Endpoint unreachable — fall through to the generic error
        // display so the user sees SOMETHING rather than a silent
        // hang.
      }

      // Tell the backend this attempt failed so the counter increments.
      // Done as fire-and-forget; we don't block the error display on it.
      try {
        const r = await fetch(`${apiBase}/api/auth/login-failed`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ email: cleanEmail }),
        });
        if (r.ok) {
          const fb = await r.json();
          if (fb && typeof fb.attempts_remaining === "number" && fb.attempts_remaining <= 2 && fb.attempts_remaining > 0) {
            // Soft warning when nearly locked out — gives a heads-up
            // before the next attempt fully blocks the account.
            setGeneralError(
              `${error.message} (${fb.attempts_remaining} attempt${fb.attempts_remaining > 1 ? "s" : ""} remaining before temporary lockout)`
            );
            setEmailLoading(false);
            return;
          }
        }
      } catch {}
      setGeneralError(error.message);
      setEmailLoading(false);
      return;
    }

    // On success, reset the per-email failure counter so a user who
    // recovered from typos isn't punished going forward.
    try {
      await fetch(`${apiBase}/api/auth/login-success`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email: cleanEmail }),
      });
    } catch {}

    // Ban check: query /api/auth/ban-status/{uid} after successful auth.
    // If the account is banned, redirect to /banned before reaching the
    // dashboard. Also handles backends that surface bans as 403 BANNED.
    const userId = data?.user?.id;
    if (userId) {
      try {
        const banRes = await fetch(
          `${apiBase}/api/auth/ban-status/${encodeURIComponent(userId)}`,
          { cache: "no-store" },
        );
        if (banRes.ok) {
          const banData = await banRes.json();
          if (banData?.banned) {
            router.replace(`/banned?user=${encodeURIComponent(userId)}`);
            return;
          }
        } else if (banRes.status === 403) {
          const banBody = await banRes.json().catch(() => ({}));
          if (banBody?.detail === "BANNED") {
            const uid = banBody?.user_id || userId;
            router.replace(`/banned?user=${encodeURIComponent(uid)}`);
            return;
          }
        }
      } catch {
        // Ban-status endpoint unavailable — fail open. The dashboard
        // layout will catch the 403 on its /api/profile/me fetch.
      }
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
        // /api/tenant/by-email/ is no longer public — it requires a JWT
        // whose email claim matches the requested address. We just
        // resolved Supabase signInWithPassword above so a session
        // exists; authFetch pulls session.access_token and adds the
        // Bearer header automatically (don't pass the token explicitly
        // since data.session can be momentarily null while Supabase
        // hydrates getSession() under the hood).
        const res = await authFetch(
          `${apiBase}/api/tenant/by-email/${encodeURIComponent(userEmail)}`,
        );
        if (res.status === 403) {
          const body = await res.json().catch(() => ({}));
          if (body?.detail === "BANNED") {
            const uid = body?.user_id || userId;
            if (uid) router.replace(`/banned?user=${encodeURIComponent(uid)}`);
            return;
          }
        }
        const json = await res.json().catch(() => ({}));
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
          <img src="/logo.png" alt="ARIA" className="h-14 w-14 rounded-full object-cover shadow-lg shadow-[#534AB7]/20" />
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
              <a href="/reset-password" className="text-xs text-[#534AB7] hover:underline">
                Forgot password?
              </a>
            </div>
            <div className="relative">
              <input
                id="password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                required
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

        {/* Google OAuth — secondary, smaller. Kept for users still on
            Google's Test-mode allowlist; primary login is now email +
            password. */}
        <button
          type="button"
          onClick={handleGoogleSignIn}
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
      </div>

      {/* Sign up link */}
      <p className="text-sm text-[#5F5E5A] text-center mt-6">
        Don&apos;t have an account?{" "}
        <a href="/signup" className="text-[#534AB7] font-semibold hover:underline">Sign up</a>
      </p>
    </div>
  );
}
