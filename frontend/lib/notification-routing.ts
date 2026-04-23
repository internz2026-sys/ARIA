/**
 * Centralized routing logic for notification deep-links.
 *
 * Every notification carries (at minimum) a category plus an optional
 * resource_type + resource_id. This module converts that shape into
 * the client-side URL the NotificationBell should navigate to when
 * the user clicks the notification.
 *
 * Why it's here and not inline in NotificationBell:
 *   - Folder-structure changes (e.g. renaming /dashboard/inbox to
 *     /dashboard/my-queue) only need updating in one place.
 *   - The same mapping is used by the deleted-item toast ("item no
 *     longer available" → shows the page the user meant to land on).
 *   - Future deep-link entry points (email click, in-app alert,
 *     push notification) can reuse this without copy-pasting the
 *     category → path table.
 *
 * Route group `(dashboard)` is stripped from URLs by Next.js, so
 * /inbox (not /dashboard/inbox) is the live path. All routes here
 * use relative /-prefixed paths so the same code works in local,
 * staging, and production without rewriting.
 */

export type NotificationLike = {
  href?: string | null;
  category?: string | null;
  resource_type?: string | null;
  resource_id?: string | null;
  metadata?: Record<string, unknown> | null;
};

/** Map a resource_type (backend string) to the frontend route group. */
const RESOURCE_TYPE_PATH: Record<string, string> = {
  // Inbox family
  inbox_item: "/inbox",
  email_draft: "/inbox",
  email_sequence: "/inbox",
  social_post: "/inbox",
  blog_post: "/inbox",
  article: "/inbox",
  landing_page: "/inbox",
  ad_campaign: "/inbox",
  image: "/inbox",
  media: "/inbox",
  // Projects / kanban
  project: "/projects",
  task: "/projects",
  // CRM
  crm_contact: "/crm",
  contact: "/crm",
  crm_company: "/crm",
  company: "/crm",
  crm_deal: "/crm",
  deal: "/crm",
  // Conversations
  email_thread: "/conversations",
  conversation: "/conversations",
  whatsapp_thread: "/conversations",
  // Scheduling
  scheduled_task: "/calendar",
  schedule: "/calendar",
  // Campaigns
  campaign: "/campaigns",
  // Agents / system
  agent: "/agents",
  agent_log: "/agents",
  integration: "/settings",
  system: "/settings",
};

/** Map a legacy `category` (older notifications) to a page. */
const CATEGORY_PATH: Record<string, string> = {
  inbox: "/inbox",
  conversation: "/conversations",
  status: "/calendar",
  system: "/settings",
  crm: "/crm",
  project: "/projects",
};

/**
 * Resolve the target URL for a notification click. Priority:
 *   1. An explicit `href` set by the backend (most specific — may
 *      already include ?id=… query params).
 *   2. `resource_type` + `resource_id` mapping (new universal shape).
 *   3. `metadata.resource_type` + `metadata.resource_id` (some older
 *      notify call sites stash the id there instead of at top level).
 *   4. `category` fallback (legacy notifications).
 *   5. Hard fallback to `/dashboard`.
 *
 * Absolute URLs (http://…) are rejected — the notification could
 * theoretically carry one from a prompt-injection-ish payload, and
 * we never want to bounce the user off the app.
 */
export function getRouteForItem(n: NotificationLike | null | undefined): string {
  if (!n) return "/dashboard";

  // 1. Explicit href wins.
  if (n.href && n.href.startsWith("/")) return n.href;

  // 2. resource_type + resource_id on the notification itself.
  const directType = (n.resource_type || "").trim();
  const directId = (n.resource_id || "").trim();
  if (directType && RESOURCE_TYPE_PATH[directType]) {
    const base = RESOURCE_TYPE_PATH[directType];
    return directId ? `${base}?id=${encodeURIComponent(directId)}` : base;
  }

  // 3. metadata-nested shape.
  const md = n.metadata;
  if (md && typeof md === "object") {
    const mdType = String((md as any).resource_type || "").trim();
    const mdId = String((md as any).resource_id || (md as any).item_id || (md as any).inbox_item_id || "").trim();
    if (mdType && RESOURCE_TYPE_PATH[mdType]) {
      const base = RESOURCE_TYPE_PATH[mdType];
      return mdId ? `${base}?id=${encodeURIComponent(mdId)}` : base;
    }
    // No resource_type but we have an id and a known category → assume
    // inbox (most common) or use category's page.
    if (mdId) {
      const catBase = CATEGORY_PATH[(n.category || "").trim()];
      if (catBase) return `${catBase}?id=${encodeURIComponent(mdId)}`;
    }
  }

  // 4. Category fallback.
  const cat = (n.category || "").trim();
  if (CATEGORY_PATH[cat]) return CATEGORY_PATH[cat];

  // 5. Final fallback.
  return "/dashboard";
}

/**
 * Extract the resource id from a route produced by getRouteForItem,
 * or from an arbitrary URL with ?id=…. Used by the page-level deep-
 * link handlers (Inbox, Projects, CRM, Calendar) so they share the
 * same parsing code.
 */
export function extractIdFromRoute(url: string): string {
  try {
    const q = url.includes("?") ? url.slice(url.indexOf("?")) : "";
    const sp = new URLSearchParams(q);
    return sp.get("id") || "";
  } catch {
    return "";
  }
}
