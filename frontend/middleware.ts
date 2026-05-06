import { NextRequest, NextResponse } from "next/server";

// Canonical production host. Any request hitting the raw VPS IP, the
// old sslip.io hostname, or the :3000 dev URL gets 301-redirected here
// so the browser upgrades to HTTPS + the new branded domain.
// Internal docker-to-docker traffic uses container names (frontend:3000,
// aria-nginx, etc.) — those hosts don't match and pass through untouched.
const CANONICAL_HOST = "aria.hoversight.agency";

const LEGACY_HOST_PATTERNS = [
  /^72\.61\.126\.188(?::\d+)?$/,  // Raw IP, any port
  /^72-61-126-188\.sslip\.io$/,   // Old sslip.io hostname pre-domain-migration
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
