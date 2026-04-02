"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import { API_URL } from "@/lib/api";

export default function AuthCallbackPage() {
  const router = useRouter();

  useEffect(() => {
    async function handleAuth() {
      // Use onAuthStateChange to get the session — it includes
      // provider_token and provider_refresh_token from the OAuth redirect,
      // whereas getSession() returns a cached session without them.
      let handled = false;
      const { data: { subscription } } = supabase.auth.onAuthStateChange(
        async (_event, newSession) => {
          if (newSession && !handled) {
            handled = true;
            subscription.unsubscribe();
            await processSession(newSession);
          }
        }
      );

      // Fallback: if no auth event fires within 3s, try getSession
      setTimeout(async () => {
        if (handled) return;
        const { data: { session } } = await supabase.auth.getSession();
        if (session && !handled) {
          handled = true;
          subscription.unsubscribe();
          await processSession(session);
        }
      }, 3000);
    }

    async function storeGoogleTokens(
      tenantId: string,
      providerToken: string | null | undefined,
      providerRefreshToken: string | null | undefined,
    ) {
      if (!providerToken) return;
      try {
        await fetch(`${API_URL}/api/integrations/${tenantId}/google-tokens`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            google_access_token: providerToken,
            google_refresh_token: providerRefreshToken || null,
          }),
        });
      } catch {
        // Non-blocking — tokens will be captured on next login
      }
    }

    async function processSession(session: {
      user: { created_at: string; email?: string };
      provider_token?: string | null;
      provider_refresh_token?: string | null;
    }) {
      const params = new URLSearchParams(window.location.search);
      const mode = params.get("mode");

      if (mode === "login") {
        const createdAt = new Date(session.user.created_at).getTime();
        const isNewAccount = Date.now() - createdAt < 60_000;
        if (isNewAccount) {
          await supabase.auth.signOut();
          router.replace("/login?error=no_account");
          return;
        }
      }

      // Check if user already has a tenant config (server-side, survives localStorage clears)
      const email = session.user.email;
      if (email) {
        try {
          const res = await fetch(`${API_URL}/api/tenant/by-email/${encodeURIComponent(email)}`);
          const data = await res.json();
          if (data.tenant_id) {
            // Restore tenant_id — user already completed onboarding
            localStorage.setItem("aria_tenant_id", data.tenant_id);
            // Store Google OAuth tokens for Gmail sending
            await storeGoogleTokens(data.tenant_id, session.provider_token, session.provider_refresh_token);
            router.replace("/dashboard");
            return;
          }
        } catch {
          // If backend is down, fall through to localStorage check
        }
      }

      // No server-side config found — check localStorage as fallback
      const tenantId = localStorage.getItem("aria_tenant_id");
      if (tenantId) {
        // Store Google OAuth tokens for Gmail sending
        await storeGoogleTokens(tenantId, session.provider_token, session.provider_refresh_token);
        router.replace("/dashboard");
        return;
      }

      // No config anywhere — go to onboarding (tokens will be stored on first login after onboarding)
      // Save tokens temporarily so they can be stored after onboarding completes
      if (session.provider_token) {
        localStorage.setItem("aria_google_token", session.provider_token);
        if (session.provider_refresh_token) {
          localStorage.setItem("aria_google_refresh_token", session.provider_refresh_token);
        }
      }
      router.replace("/welcome");
    }

    handleAuth();
  }, [router]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-[#F8F8F6]">
      <div className="flex flex-col items-center gap-3">
        <div className="w-10 h-10 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
        <p className="text-sm text-[#5F5E5A]">Completing sign in...</p>
      </div>
    </div>
  );
}
