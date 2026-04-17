import { NextRequest, NextResponse } from "next/server";

// Canonical production host. Any request hitting the raw VPS IP or the old
// :3000 URL gets 301-redirected here so the browser upgrades to HTTPS.
// Internal docker-to-docker traffic uses container names (frontend:3000,
// aria-nginx, etc.) — those hosts don't match and pass through untouched.
const CANONICAL_HOST = "72-61-126-188.sslip.io";

const LEGACY_HOST_PATTERNS = [
  /^72\.61\.126\.188(?::\d+)?$/,  // Raw IP, any port
];

export function middleware(req: NextRequest) {
  const host = req.headers.get("host") || "";
  if (LEGACY_HOST_PATTERNS.some((re) => re.test(host))) {
    const url = new URL(req.url);
    url.protocol = "https:";
    url.host = CANONICAL_HOST;
    url.port = "";
    return NextResponse.redirect(url, 301);
  }
  return NextResponse.next();
}

export const config = {
  // Match everything except Next internals and static assets.
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
