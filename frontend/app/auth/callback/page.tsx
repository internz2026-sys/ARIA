"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import { API_URL, authFetch } from "@/lib/api";

export default function AuthCallbackPage() {
  const router = useRouter();

  useEffect(() => {
    // ── OAuth ban short-circuit (must run FIRST) ────────────────────────
    // When the Supabase Google OAuth flow rejects a banned user it
    // redirects back to /auth/callback with:
    //   ?error=access_denied&error_code=user_banned&error_description=...
    // The rest of this page assumes a valid session is coming and falls
    // back to /login after a 4s timeout — leaving banned users with no
    // explanation. Detect the ban here and route to /banned instead.
    //
    // The OAuth error URL has NO email/uid, so we ask the backend for
    // the most-recently-banned profile row (close enough for solo and
    // small-team use) and redirect to /banned?user=<uid>. That lets the
    // /banned page render the full ban detail (email + reason +
    // duration) instead of the generic "contact support" placeholder.
    const queryParams = new URLSearchParams(window.location.search);
    const oauthErrorCode = queryParams.get("error_code");
    const oauthError = queryParams.get("error");
    if (oauthErrorCode === "user_banned" || (oauthError === "access_denied" && oauthErrorCode === "user_banned")) {
      console.log("[ARIA Auth] OAuth flow rejected with user_banned — resolving uid + routing to /banned");
      (async () => {
        try {
          const r = await fetch(`${API_URL}/api/auth/most-recent-banned`, { cache: "no-store" });
          if (r.ok) {
            const d = await r.json();
            if (d?.banned && d?.user_id) {
              router.replace(`/banned?user=${encodeURIComponent(d.user_id)}`);
              return;
            }
          }
        } catch {
          // Endpoint unreachable — fall through to the generic state.
        }
        // Couldn't resolve a uid → show the generic suspension UI.
        router.replace("/banned?source=signin");
      })();
      return;
    }

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

    // INTENTIONALLY REMOVED: the previous version of this page wrote
    // `session.provider_token` (the Supabase Google OAuth token) into
    // tenant_configs.google_access_token here. That worked back when
    // sign-in itself requested Gmail scopes — the provider_token had
    // gmail.send + gmail.readonly so storing it doubled as the Gmail
    // integration's token.
    //
    // Sign-in now requests only basic profile + email scopes (so the
    // OAuth client doesn't trip Google's sensitive-scope verification
    // gate). The provider_token returned by Supabase sign-in no longer
    // has Gmail access, and overwriting tenant_configs with it would
    // CLOBBER the working Gmail token that the dedicated Settings →
    // Connect Gmail flow had stored. That manifested as a backend 403
    // "token may lack required scopes" the next time the agent tried
    // to fetch a Gmail thread, and is why Reconnect "stopped sticking"
    // after a re-login.
    //
    // Gmail tokens now ONLY come from /api/auth/google/connect/{tenant_id}
    // (Settings → Integrations). Sign-in is no longer involved.

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
          // /api/tenant/by-email/ is no longer public — it requires a JWT
          // whose email claim matches the requested address. We just
          // completed Supabase OAuth so a session exists; authFetch
          // grabs the access_token and adds the Bearer header.
          const res = await authFetch(`${API_URL}/api/tenant/by-email/${encodeURIComponent(email)}`);
          const data = await res.json();
          if (data.tenant_id) {
            localStorage.setItem("aria_tenant_id", data.tenant_id);
            router.replace("/dashboard");
            return;
          } else {
            // This user has no tenant — clear any stale tenant_id from a previous user
            localStorage.removeItem("aria_tenant_id");
          }
        } catch {
          // Backend down — fall through to localStorage check
        }
      }

      // No legacy provider-token persistence here either: Gmail tokens
      // come from the dedicated /api/auth/google/connect/{tenant_id}
      // flow, not from the sign-in OAuth.
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
