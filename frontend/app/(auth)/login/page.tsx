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
  const [generalError, setGeneralError] = useState("");
  const [loading, setLoading] = useState(false);

  // Check for OAuth error in URL params
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

  async function handleGoogleSignIn() {
    setGeneralError("");
    setLoading(true);
    const { error } = await supabase.auth.signInWithOAuth({
      provider: "google",
      options: {
        redirectTo: `${window.location.origin}/auth/callback?mode=login`,
        scopes: "https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/gmail.readonly",
        queryParams: {
          access_type: "offline",
          prompt: "consent",
        },
      },
    });
    if (error) {
      setGeneralError(error.message);
      setLoading(false);
    }
  }

  return (
    <div className="w-full max-w-[420px] mx-auto px-6 py-10">
      <div className="bg-white rounded-2xl border border-[#E0DED8] shadow-sm p-8">
        {/* Logo */}
        <div className="flex justify-center mb-6">
          <div className="w-14 h-14 rounded-2xl bg-[#534AB7] flex items-center justify-center shadow-lg shadow-[#534AB7]/20">
            <span className="text-white text-2xl font-bold">A</span>
          </div>
        </div>

        <h1 className="text-[26px] font-bold text-[#2C2C2A] text-center mb-1">Welcome back</h1>
        <p className="text-[#5F5E5A] text-center mb-8 text-[15px]">Sign in to your ARIA account</p>

        {/* General error */}
        {generalError && (
          <div className="mb-5 p-3 rounded-lg bg-[#FEF2EE] border border-[#F5C6B3] text-[#D85A30] text-sm">
            {generalError}
          </div>
        )}

        {/* Google OAuth */}
        <button
          type="button"
          onClick={handleGoogleSignIn}
          disabled={loading}
          className="w-full flex items-center justify-center gap-2.5 border border-[#E0DED8] rounded-lg h-12 text-sm font-medium text-[#2C2C2A] hover:bg-[#F8F8F6] transition disabled:opacity-60 disabled:cursor-not-allowed"
        >
          {loading ? (
            <div className="w-5 h-5 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
          ) : (
            <GoogleIcon />
          )}
          {loading ? "Redirecting..." : "Continue with Google"}
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
