"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import { API_URL } from "@/lib/api";

export default function AuthCallbackPage() {
  const router = useRouter();

  useEffect(() => {
    // ── Extract Google tokens from URL hash (Supabase implicit flow) ──
    // Supabase puts provider_token in the hash fragment after OAuth redirect.
    // We capture it here because getSession()/onAuthStateChange may not include it.
    function extractTokensFromHash(): { providerToken: string | null; providerRefreshToken: string | null } {
      const hash = window.location.hash.substring(1);
      const params = new URLSearchParams(hash);
      return {
        providerToken: params.get("provider_token"),
        providerRefreshToken: params.get("provider_refresh_token"),
      };
    }

    const hashTokens = extractTokensFromHash();
    console.log("[ARIA Auth] Hash tokens:", hashTokens.providerToken ? "present" : "null", hashTokens.providerRefreshToken ? "present" : "null");
    console.log("[ARIA Auth] URL hash:", window.location.hash.substring(0, 100));

    async function handleAuth() {
      let handled = false;

      const { data: { subscription } } = supabase.auth.onAuthStateChange(
        async (event, newSession) => {
          console.log("[ARIA Auth] Event:", event, "provider_token:", newSession?.provider_token ? "present" : "null");
          if (!newSession || handled) return;

          // Prefer the SIGNED_IN event (has provider tokens) over INITIAL_SESSION
          const hasProviderToken = !!(
            newSession.provider_token ||
            hashTokens.providerToken
          );

          // Skip INITIAL_SESSION if it has no provider token — wait for SIGNED_IN
          if (event === "INITIAL_SESSION" && !hasProviderToken) return;

          handled = true;
          subscription.unsubscribe();

          // Merge hash tokens into session if session doesn't have them
          const sessionWithTokens = {
            ...newSession,
            provider_token: newSession.provider_token || hashTokens.providerToken,
            provider_refresh_token: newSession.provider_refresh_token || hashTokens.providerRefreshToken,
          };
          console.log("[ARIA Auth] Final provider_token:", sessionWithTokens.provider_token ? "present" : "null");

          await processSession(sessionWithTokens);
        }
      );

      // Fallback: if no SIGNED_IN fires within 4s, proceed with whatever we have
      setTimeout(async () => {
        if (handled) return;
        console.log("[ARIA Auth] Fallback triggered after 4s");
        handled = true;
        subscription.unsubscribe();
        const { data: { session } } = await supabase.auth.getSession();
        if (session) {
          const sessionWithTokens = {
            ...session,
            provider_token: session.provider_token || hashTokens.providerToken,
            provider_refresh_token: session.provider_refresh_token || hashTokens.providerRefreshToken,
          };
          console.log("[ARIA Auth] Fallback provider_token:", sessionWithTokens.provider_token ? "present" : "null");
          await processSession(sessionWithTokens);
        } else {
          router.replace("/login");
        }
      }, 4000);
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
        // Non-blocking
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

      // Check if user already has a tenant config
      const email = session.user.email;
      if (email) {
        try {
          const res = await fetch(`${API_URL}/api/tenant/by-email/${encodeURIComponent(email)}`);
          const data = await res.json();
          if (data.tenant_id) {
            localStorage.setItem("aria_tenant_id", data.tenant_id);
            await storeGoogleTokens(data.tenant_id, session.provider_token, session.provider_refresh_token);
            router.replace("/dashboard");
            return;
          }
        } catch {
          // Backend down — fall through
        }
      }

      // Check localStorage
      const tenantId = localStorage.getItem("aria_tenant_id");
      if (tenantId) {
        await storeGoogleTokens(tenantId, session.provider_token, session.provider_refresh_token);
        router.replace("/dashboard");
        return;
      }

      // New user — save tokens for after onboarding
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
