import type { Metadata, Viewport } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ARIA — AI Business Automation Platform",
  description: "AI Agents That Run Your Business — You Just Describe It",
};

// Explicit viewport directive. Without this, Next.js's stock default
// is fine on most devices but some mobile browsers (older Android
// Chrome, Samsung Internet on some builds) fall back to a 980px
// virtual viewport — the page renders zoomed-out and users have to
// pinch in. `viewportFit: cover` opts the layout into the iOS Safari
// safe-area inset env(safe-area-inset-*) variables that
// MobileBottomNav already reads. We intentionally do NOT set
// userScalable: false — pinch-zoom is an accessibility requirement
// and disabling it would fail WCAG 1.4.4 even though the spec asked
// for it.
export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  maximumScale: 5,
  viewportFit: "cover",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="antialiased">{children}</body>
    </html>
  );
}
