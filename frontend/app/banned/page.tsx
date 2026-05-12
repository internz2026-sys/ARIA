"use client";

/**
 * /banned — shown when a user's account has been banned.
 *
 * Reads ?user=<uid> from the URL, fetches /api/auth/ban-status/{uid},
 * and renders a dark-themed page matching the marketing site palette.
 *
 * Edge cases:
 *  - banned: false   → redirect to /login (user isn't actually banned)
 *  - fetch fails     → render a generic "unavailable" message (no crash)
 *  - no ?user param  → render the generic message too
 *
 * This page is intentionally OUTSIDE all route groups so it requires no
 * authentication and no dashboard layout wrapping.
 */

import { useEffect, useState, Suspense } from "react";
import { useSearchParams } from "next/navigation";
import Link from "next/link";

const FONTS_LINK =
  "https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600&family=Sora:wght@300;400;500;600;700&display=swap";

function loadFonts() {
  if (typeof document === "undefined") return;
  if (!document.querySelector(`link[href*="Instrument+Serif"]`)) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = FONTS_LINK;
    document.head.appendChild(link);
  }
}

type BanStatus = {
  banned: boolean;
  banned_at: string | null;
  banned_until: string | null;
  indefinite: boolean;
  reason: string | null;
};

type PageState =
  | { kind: "loading" }
  | { kind: "banned"; status: BanStatus }
  | { kind: "banned_anonymous" } // Banned but no uid available (OAuth flow)
  | { kind: "not_banned" }
  | { kind: "unavailable"; error?: string };

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString("en-US", {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
  } catch {
    return iso;
  }
}

function BannedContent() {
  const searchParams = useSearchParams();
  const userId = searchParams.get("user");
  const [state, setState] = useState<PageState>({ kind: "loading" });
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    loadFonts();
    setTimeout(() => setLoaded(true), 80);
  }, []);

  const source = searchParams.get("source");

  useEffect(() => {
    if (!userId) {
      // No uid in URL. Two cases:
      //   - source=signin: the OAuth callback bounced a banned user
      //     here without a uid (the OAuth error response doesn't
      //     include one). Render a dedicated "your account is
      //     suspended" message so the user knows WHY they were
      //     redirected, with a contact-support CTA.
      //   - no source: someone landed on /banned directly. Show the
      //     generic unavailable state.
      if (source === "signin") {
        setState({ kind: "banned_anonymous" });
      } else {
        setState({ kind: "unavailable" });
      }
      return;
    }

    const apiBase = process.env.NEXT_PUBLIC_API_URL || "";
    fetch(`${apiBase}/api/auth/ban-status/${encodeURIComponent(userId)}`, {
      cache: "no-store",
    })
      .then(async (res) => {
        if (!res.ok) {
          setState({ kind: "unavailable", error: `Status ${res.status}` });
          return;
        }
        const data: BanStatus = await res.json();
        if (!data.banned) {
          // User isn't (or no longer) banned — surface that as its own
          // state with an explicit "Sign in" button. Auto-redirecting
          // back to /login confused users who landed here after a
          // failed login: they bounced silently and didn't realize the
          // ban had been lifted.
          setState({ kind: "not_banned" });
          return;
        }
        setState({ kind: "banned", status: data });
      })
      .catch((err) => {
        setState({ kind: "unavailable", error: err?.message });
      });
  }, [userId, source]);

  const isLoading = state.kind === "loading";

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#0A0A0F",
        color: "#F0EDE8",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: "40px 20px",
        fontFamily: "'Sora', sans-serif",
        opacity: loaded ? 1 : 0,
        transition: "opacity 0.5s ease",
      }}
    >
      {/* Gradient orb — decorative */}
      <div
        aria-hidden="true"
        style={{
          position: "fixed",
          top: -200,
          left: "50%",
          transform: "translateX(-50%)",
          width: 600,
          height: 600,
          background: "radial-gradient(circle, rgba(233,69,96,0.07) 0%, transparent 70%)",
          pointerEvents: "none",
          zIndex: 0,
        }}
      />

      <div
        style={{
          position: "relative",
          zIndex: 1,
          maxWidth: 520,
          width: "100%",
          background: "rgba(255,255,255,0.02)",
          border: "1px solid rgba(255,255,255,0.07)",
          borderRadius: 20,
          padding: "48px 40px",
          textAlign: "center",
        }}
      >
        {isLoading ? (
          /* Loading spinner */
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              alignItems: "center",
              gap: 16,
              padding: "24px 0",
            }}
          >
            <div
              style={{
                width: 36,
                height: 36,
                borderRadius: "50%",
                border: "2px solid rgba(233,69,96,0.3)",
                borderTopColor: "#E94560",
                animation: "spin 0.8s linear infinite",
              }}
            />
            <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
            <p style={{ color: "rgba(240,237,232,0.4)", fontSize: 14 }}>Checking account status...</p>
          </div>
        ) : state.kind === "unavailable" ? (
          /* Generic unavailable state */
          <>
            <div
              style={{
                width: 52,
                height: 52,
                borderRadius: "50%",
                background: "rgba(233,69,96,0.1)",
                border: "1px solid rgba(233,69,96,0.2)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "0 auto 24px",
              }}
            >
              <svg width="24" height="24" fill="none" stroke="#E94560" strokeWidth={1.8} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
              </svg>
            </div>

            <h1
              style={{
                fontFamily: "'Instrument Serif'",
                fontSize: 28,
                fontWeight: 400,
                lineHeight: 1.2,
                marginBottom: 16,
                color: "#F0EDE8",
              }}
            >
              Your account is unavailable
            </h1>
            <p style={{ color: "rgba(240,237,232,0.5)", fontSize: 15, lineHeight: 1.7, marginBottom: 32 }}>
              We&apos;re unable to verify your account status right now. Please contact support for assistance.
            </p>

            <SupportFooter />
          </>
        ) : state.kind === "banned_anonymous" ? (
          /* User came from the OAuth callback with error_code=user_banned
             but no uid (Supabase doesn't include one on OAuth error).
             Render the suspension UI without specific reason/until-date
             — point them at support. */
          <>
            <div
              style={{
                width: 52,
                height: 52,
                borderRadius: "50%",
                background: "rgba(233,69,96,0.1)",
                border: "1px solid rgba(233,69,96,0.25)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "0 auto 24px",
              }}
            >
              <svg width="24" height="24" fill="none" stroke="#E94560" strokeWidth={1.8} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
              </svg>
            </div>

            <h1
              style={{
                fontFamily: "'Instrument Serif'",
                fontSize: 30,
                fontWeight: 400,
                lineHeight: 1.15,
                marginBottom: 16,
                color: "#F0EDE8",
              }}
            >
              Your account has been{" "}
              <span style={{ fontStyle: "italic", color: "#E94560" }}>suspended</span>
            </h1>
            <p style={{ color: "rgba(240,237,232,0.55)", fontSize: 15, lineHeight: 1.7, marginBottom: 28 }}>
              Sign-in is disabled on this account. Contact our support team and we&apos;ll share the specific reason and next steps.
            </p>

            <SupportFooter />
          </>
        ) : state.kind === "not_banned" ? (
          /* User followed a stale /banned URL but their account isn't
             banned (anymore). Don't redirect — show a button so they
             control where they go next. */
          <>
            <div
              style={{
                width: 52,
                height: 52,
                borderRadius: "50%",
                background: "rgba(74,222,128,0.10)",
                border: "1px solid rgba(74,222,128,0.25)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "0 auto 24px",
              }}
            >
              <svg width="24" height="24" fill="none" stroke="#4ADE80" strokeWidth={1.8} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
              </svg>
            </div>

            <h1
              style={{
                fontFamily: "'Instrument Serif'",
                fontSize: 28,
                fontWeight: 400,
                lineHeight: 1.2,
                marginBottom: 16,
                color: "#F0EDE8",
              }}
            >
              Your account is in good standing
            </h1>
            <p style={{ color: "rgba(240,237,232,0.5)", fontSize: 15, lineHeight: 1.7, marginBottom: 28 }}>
              No active suspension on this account. You can sign in normally.
            </p>

            <Link
              href="/login"
              style={{
                display: "inline-block",
                fontFamily: "'Sora'",
                fontSize: 14,
                fontWeight: 500,
                background: "linear-gradient(135deg, #E94560 0%, #c73652 100%)",
                color: "#fff",
                padding: "12px 28px",
                borderRadius: 8,
                textDecoration: "none",
                marginBottom: 8,
              }}
            >
              Go to sign in
            </Link>

            <div>
              <Link
                href="/"
                style={{
                  display: "inline-block",
                  fontSize: 13,
                  color: "rgba(240,237,232,0.35)",
                  textDecoration: "none",
                  marginTop: 16,
                }}
              >
                &larr; Return home
              </Link>
            </div>
          </>
        ) : (
          /* Banned state */
          <>
            {/* Red warning icon */}
            <div
              style={{
                width: 52,
                height: 52,
                borderRadius: "50%",
                background: "rgba(233,69,96,0.1)",
                border: "1px solid rgba(233,69,96,0.25)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                margin: "0 auto 24px",
              }}
            >
              <svg width="24" height="24" fill="none" stroke="#E94560" strokeWidth={1.8} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M18.364 18.364A9 9 0 005.636 5.636m12.728 12.728A9 9 0 015.636 5.636m12.728 12.728L5.636 5.636" />
              </svg>
            </div>

            {/* Headline */}
            <h1
              style={{
                fontFamily: "'Instrument Serif'",
                fontSize: 30,
                fontWeight: 400,
                lineHeight: 1.15,
                marginBottom: 16,
                color: "#F0EDE8",
              }}
            >
              Your account has been{" "}
              <span style={{ fontStyle: "italic", color: "#E94560" }}>suspended</span>
            </h1>

            {/* Duration */}
            <div
              style={{
                background: "rgba(233,69,96,0.06)",
                border: "1px solid rgba(233,69,96,0.15)",
                borderRadius: 10,
                padding: "12px 16px",
                marginBottom: 20,
                fontFamily: "'JetBrains Mono'",
                fontSize: 13,
                color: "rgba(240,237,232,0.65)",
                letterSpacing: 0.3,
              }}
            >
              {state.status.indefinite || !state.status.banned_until
                ? "Suspended indefinitely"
                : `Suspended until ${formatDate(state.status.banned_until)}`}
            </div>

            {/* Reason */}
            <div style={{ marginBottom: 32, textAlign: "left" }}>
              <p
                style={{
                  fontSize: 12,
                  fontFamily: "'JetBrains Mono'",
                  letterSpacing: 1.5,
                  color: "rgba(240,237,232,0.3)",
                  textTransform: "uppercase",
                  marginBottom: 8,
                }}
              >
                Reason
              </p>
              <p
                style={{
                  fontSize: 15,
                  color: "rgba(240,237,232,0.6)",
                  lineHeight: 1.65,
                  fontStyle: state.status.reason ? "normal" : "italic",
                }}
              >
                {state.status.reason || "No reason provided."}
              </p>
            </div>

            <SupportFooter />
          </>
        )}
      </div>

      {/* ARIA wordmark footer */}
      <div style={{ marginTop: 32, fontFamily: "'JetBrains Mono'", fontSize: 12, color: "rgba(240,237,232,0.18)", letterSpacing: 2 }}>
        ARIA &mdash; AI Marketing Team
      </div>
    </div>
  );
}

function SupportFooter() {
  return (
    <>
      <p style={{ fontSize: 14, color: "rgba(240,237,232,0.4)", marginBottom: 20, lineHeight: 1.6 }}>
        If you believe this is a mistake, reach out to our support team and we&apos;ll look into it.
      </p>

      <a
        href="mailto:accounts@zillamedia.co"
        style={{
          display: "inline-block",
          fontFamily: "'Sora'",
          fontSize: 14,
          fontWeight: 500,
          color: "#E94560",
          textDecoration: "none",
          marginBottom: 20,
          padding: "10px 24px",
          border: "1px solid rgba(233,69,96,0.3)",
          borderRadius: 8,
          transition: "all 0.2s ease",
        }}
      >
        accounts@zillamedia.co
      </a>

      <div>
        <Link
          href="/"
          style={{
            display: "inline-block",
            fontSize: 13,
            color: "rgba(240,237,232,0.35)",
            textDecoration: "none",
          }}
        >
          &larr; Return home
        </Link>
      </div>
    </>
  );
}

// Suspense boundary because useSearchParams() requires it in Next.js 14
export default function BannedPage() {
  return (
    <Suspense
      fallback={
        <div
          style={{
            minHeight: "100vh",
            background: "#0A0A0F",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
          }}
        />
      }
    >
      <BannedContent />
    </Suspense>
  );
}
