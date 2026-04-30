"use client";

import React, { useEffect, useState, useCallback, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { API_URL, authFetch, inbox } from "@/lib/api";
import { AGENT_NAMES, AGENT_COLORS } from "@/lib/agent-config";
import EmailEditor from "@/components/shared/EmailEditor";
import { formatDateAgo } from "@/lib/utils";
import { renderMarkdown } from "@/lib/render-markdown";
import { useNotifications } from "@/lib/use-notifications";
import { useConfirm } from "@/lib/use-confirm";
import { useViewToggle } from "@/lib/use-view-toggle";

interface EmailDraft {
  to: string;
  subject: string;
  html_body: string;
  text_body: string;
  preview_snippet: string;
  status: string;
}

interface InboxItem {
  id: string;
  agent: string;
  type: string;
  title: string;
  content: string;
  status: string;
  priority: string;
  created_at: string;
  email_draft?: (EmailDraft & { image_urls?: string[] }) | null;
}

/** Extract the first attached image URL for the list row thumbnail.
 *  Sources (in priority order):
 *    1. email_draft.image_urls[0] — Email Marketer attaches media here
 *    2. parsed social_posts[0].image_url — Social Manager attaches here
 *    3. Markdown ![](url) / raw image URL in `content` — Media Designer
 *       rows and any ad-hoc image links the agent embedded inline.
 */
function getInboxThumbnail(item: InboxItem): string | null {
  if (item.email_draft?.image_urls?.[0]) return item.email_draft.image_urls[0]!;
  try {
    const s = item.content.indexOf("{");
    const e = item.content.lastIndexOf("}") + 1;
    if (s >= 0 && e > s) {
      const data = JSON.parse(item.content.substring(s, e));
      const firstWithImg = data?.posts?.find((p: any) => p?.image_url);
      if (firstWithImg?.image_url) return firstWithImg.image_url;
    }
  } catch {}
  const md = item.content.match(/!\[[^\]]*\]\((https?:\/\/[^\s)]+)\)/);
  if (md) return md[1];
  const raw = item.content.match(/https?:\/\/\S+?\.(?:png|jpg|jpeg|gif|webp)(?:\?\S*)?/i);
  return raw ? raw[0] : null;
}

/** Minimal inline markdown renderer for inbox content fallbacks.
 *  Handles: **bold**, *italic*, `code`, headings (## / ###),
 *  [text](url) links, ![alt](url) and [alt](image-url.png) inline
 *  images, bullet lists, paragraphs. Intentionally tiny — we don't
 *  want a full markdown dep just for this fallback. Returns React
 *  nodes, not an HTML string, so there's no XSS concern (React
 *  escapes text content automatically). */
function renderInlineMarkdown(text: string): React.ReactNode {
  if (!text) return null;
  const _IMG_EXT = /\.(png|jpg|jpeg|gif|webp|svg)(?:\?[^\s)]*)?$/i;
  const lines = text.split("\n");
  const blocks: React.ReactNode[] = [];
  let para: string[] = [];
  let listItems: string[] = [];

  const flushPara = () => {
    if (!para.length) return;
    blocks.push(
      <p key={`p-${blocks.length}`} className="my-2 text-[#2C2C2A] leading-relaxed">
        {renderInline(para.join(" "))}
      </p>,
    );
    para = [];
  };
  const flushList = () => {
    if (!listItems.length) return;
    blocks.push(
      <ul key={`ul-${blocks.length}`} className="list-disc pl-5 my-2 space-y-1 text-[#2C2C2A]">
        {listItems.map((li, i) => <li key={i}>{renderInline(li)}</li>)}
      </ul>,
    );
    listItems = [];
  };

  for (const rawLine of lines) {
    const line = rawLine.trimEnd();
    if (!line.trim()) { flushPara(); flushList(); continue; }
    // Headings
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      flushPara(); flushList();
      const level = h[1].length;
      const cls = level === 1 ? "text-xl font-bold mt-4 mb-2"
        : level === 2 ? "text-lg font-semibold mt-3 mb-2"
        : "text-base font-semibold mt-2 mb-1";
      blocks.push(
        React.createElement(
          `h${level}`,
          { key: `h-${blocks.length}`, className: `${cls} text-[#2C2C2A]` },
          renderInline(h[2]),
        ),
      );
      continue;
    }
    // Bullet
    const b = line.match(/^\s*[-*]\s+(.*)$/);
    if (b) { flushPara(); listItems.push(b[1]); continue; }
    flushList();
    para.push(line);
  }
  flushPara();
  flushList();
  return <>{blocks}</>;

  function renderInline(src: string): React.ReactNode {
    // Split by all inline patterns and recursively render
    const parts: React.ReactNode[] = [];
    const regex = /(!\[([^\]]*)\]\(([^)]+)\))|(\[([^\]]+)\]\(([^)]+)\))|(\*\*([^*]+)\*\*)|(`([^`]+)`)|(\*([^*]+)\*)/g;
    let last = 0;
    let m: RegExpExecArray | null;
    let key = 0;
    while ((m = regex.exec(src)) !== null) {
      if (m.index > last) parts.push(src.substring(last, m.index));
      if (m[1]) {
        // ![alt](url) — image
        parts.push(
          <img key={key++} src={m[3]} alt={m[2] || ""}
            className="my-2 rounded-lg max-w-full max-h-[400px] border border-[#E0DED8]"
            loading="lazy" />,
        );
      } else if (m[4]) {
        // [text](url) — if url looks like an image, render as image; else link
        if (_IMG_EXT.test(m[6])) {
          parts.push(
            <img key={key++} src={m[6]} alt={m[5] || ""}
              className="my-2 rounded-lg max-w-full max-h-[400px] border border-[#E0DED8]"
              loading="lazy" />,
          );
        } else {
          parts.push(
            <a key={key++} href={m[6]} target="_blank" rel="noopener noreferrer"
              className="text-[#534AB7] hover:underline">{m[5]}</a>,
          );
        }
      } else if (m[7]) {
        parts.push(<strong key={key++} className="font-semibold">{m[8]}</strong>);
      } else if (m[9]) {
        parts.push(
          <code key={key++} className="px-1.5 py-0.5 rounded bg-[#F8F8F6] border border-[#E0DED8] text-xs">{m[10]}</code>,
        );
      } else if (m[11]) {
        parts.push(<em key={key++} className="italic">{m[12]}</em>);
      }
      last = m.index + m[0].length;
    }
    if (last < src.length) parts.push(src.substring(last));
    return parts;
  }
}

const STATUS_TABS = [
  { key: "", label: "All" },
  { key: "processing", label: "In progress" },
  { key: "ready", label: "Content ready" },
  { key: "draft_pending_approval", label: "Pending approval" },
  { key: "needs_review", label: "Needs review" },
  { key: "sent", label: "Sent" },
  { key: "completed", label: "Completed" },
  { key: "cancelled", label: "Cancelled" },
];

const TYPE_LABELS: Record<string, string> = {
  blog_post: "Blog Post",
  email_sequence: "Email",
  email_reply: "Email Reply",
  social_post: "Social Post",
  ad_campaign: "Ad Campaign",
  strategy_update: "Strategy Update",
  whatsapp_message: "WhatsApp",
  general: "General",
};

// Fall back to a humanized slug for any type the backend introduces
// that isn't yet in TYPE_LABELS. Was previously rendering raw slugs
// like "follow_up_task" in the UI; now becomes "Follow Up Task".
function typeLabel(type: string): string {
  if (TYPE_LABELS[type]) return TYPE_LABELS[type];
  return (type || "Item")
    .replace(/_/g, " ")
    .split(" ")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(" ");
}

const PRIORITY_DOT: Record<string, string> = {
  high: "bg-red-500",
  medium: "bg-amber-400",
  low: "bg-green-500",
};

const STATUS_BADGES: Record<string, { label: string; bg: string; text: string; border: string }> = {
  processing: { label: "In progress...", bg: "bg-purple-50", text: "text-purple-600", border: "border-purple-200" },
  ready: { label: "Ready", bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200" },
  draft_pending_approval: { label: "Pending approval", bg: "bg-amber-50", text: "text-amber-700", border: "border-amber-200" },
  needs_review: { label: "Needs review", bg: "bg-orange-50", text: "text-orange-600", border: "border-orange-200" },
  sending: { label: "Sending...", bg: "bg-blue-50", text: "text-blue-600", border: "border-blue-200" },
  sent: { label: "Sent", bg: "bg-emerald-50", text: "text-emerald-700", border: "border-emerald-200" },
  completed: { label: "Completed", bg: "bg-blue-50", text: "text-blue-600", border: "border-blue-200" },
  failed: { label: "Failed", bg: "bg-red-50", text: "text-red-600", border: "border-red-200" },
  cancelled: { label: "Cancelled", bg: "bg-gray-50", text: "text-gray-500", border: "border-gray-200" },
};

const timeAgo = formatDateAgo;

function stripHtml(html: string): string {
  return html
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "")
    .replace(/<script[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>/gi, "\n\n")
    .replace(/<\/div>/gi, "\n")
    .replace(/<\/li>/gi, "\n")
    .replace(/<\/h[1-6]>/gi, "\n\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function looksLikeHtml(text: string): boolean {
  return /<\/?[a-z][\s\S]*>/i.test(text);
}

/** Build a plain-text excerpt of an inbox item's body for the list-row
 *  preview. The list cards previously only showed the title (plus an
 *  email preview snippet for email rows), which made non-email rows
 *  look empty — Content Writer / Social Manager / image rows just
 *  rendered the title with no hint of what was inside. This pulls the
 *  first line or two of body text so each card communicates its actual
 *  content at a glance.
 *
 *  Handles three shapes the `content` column can take:
 *    1. Raw HTML (e.g., email_marketer rows mirrored into content)
 *       → strip tags via stripHtml
 *    2. Social Manager JSON blob (`{"posts": [...]}`)
 *       → extract the first post's text
 *    3. Plain text or markdown
 *       → strip leading markdown decoration (`## `, `**`, `*`, `-`,
 *         backticks, image/link syntax) so the excerpt reads naturally
 *
 *  Returns null when there's nothing useful to show (empty content, or
 *  the content is purely an image URL with no surrounding prose). The
 *  caller skips rendering the line in that case so we don't show an
 *  empty `<p>` tag. */
function getInboxExcerpt(item: InboxItem): string | null {
  const raw = (item.content || "").trim();
  if (!raw) return null;

  // Social Manager / similar agents stash a {"posts": [...]} blob in
  // content. Pull the first post's text rather than showing literal
  // JSON to the user.
  if (raw.startsWith("{") && raw.includes('"posts"')) {
    try {
      const data = JSON.parse(raw);
      const firstText = data?.posts?.[0]?.text || data?.posts?.[0]?.body;
      if (typeof firstText === "string" && firstText.trim()) {
        return firstText.trim().slice(0, 220);
      }
    } catch {
      // Malformed JSON — fall through to plain-text handling below.
    }
  }

  let text = raw;
  if (looksLikeHtml(text)) text = stripHtml(text);

  // Drop pure image-only content (a single ![](url) or bare URL) —
  // the list-row thumbnail already conveys it; an empty excerpt is
  // better than the literal markdown.
  const imageOnly =
    /^!\[[^\]]*\]\(\S+\)\s*$/.test(text) ||
    /^https?:\/\/\S+?\.(?:png|jpg|jpeg|gif|webp)(?:\?\S*)?\s*$/i.test(text);
  if (imageOnly) return null;

  text = text
    // Strip inline markdown decoration so the excerpt reads as prose.
    .replace(/!\[[^\]]*\]\([^)]+\)/g, "")          // images
    .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")        // [text](url) -> text
    .replace(/^#{1,6}\s+/gm, "")                    // ## heading -> heading
    .replace(/\*\*([^*]+)\*\*/g, "$1")              // **bold** -> bold
    .replace(/(?<!\*)\*([^*\n]+)\*(?!\*)/g, "$1")   // *italic* -> italic
    .replace(/`([^`]+)`/g, "$1")                    // `code` -> code
    .replace(/^\s*[-*+]\s+/gm, "")                  // bullet markers
    .replace(/\n{2,}/g, " · ")                      // paragraph breaks -> mid-dot
    .replace(/\s+/g, " ")
    .trim();

  // Avoid duplicating the title verbatim in the excerpt.
  if (text && item.title && text.toLowerCase().startsWith(item.title.toLowerCase())) {
    text = text.slice(item.title.length).replace(/^[\s.,:;—-]+/, "").trim();
  }

  if (!text) return null;
  return text.length > 220 ? text.slice(0, 220).trimEnd() + "…" : text;
}

export default function InboxPage() {
  const { showToast } = useNotifications();
  const { confirm } = useConfirm();
  // URL state: page/tab/id are stored in query params so refresh +
  // shareable links + browser back-button all work. Read once on
  // mount, then write back via history.replaceState as state changes.
  const initialUrlState = (() => {
    if (typeof window === "undefined") return { tab: "", page: 1, id: "" };
    const sp = new URLSearchParams(window.location.search);
    return {
      tab: sp.get("tab") || "",
      page: Math.max(1, parseInt(sp.get("page") || "1", 10) || 1),
      id: sp.get("id") || "",
    };
  })();
  // Selected index for keyboard navigation (j/k). Tracked separately
  // from `selected` so the user can move focus without committing
  // to opening the detail pane on every step.
  const [keyboardIndex, setKeyboardIndex] = useState(0);
  const [items, setItems] = useState<InboxItem[]>([]);
  const [activeTab, setActiveTab] = useState(initialUrlState.tab);
  const [selected, setSelected] = useState<InboxItem | null>(null);
  // Mobile master/detail: when an item is tapped on a phone, show only the
  // detail pane and a "Back to inbox" header. The previous design rendered
  // the detail pane as `hidden md:flex`, so on mobile users could tap items
  // but never read or approve them.
  // Shared with Conversations + future master-detail pages via
  // `lib/use-view-toggle.tsx`. Keeps the toggle behavior in lockstep
  // so a fix to one place (e.g. safe-area padding adjustments) propagates
  // automatically. Existing setMobileShowDetail callsites still work
  // via the raw setter the hook exposes.
  const { mobileShowDetail, setMobileShowDetail } = useViewToggle();
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set());
  // Delete Mode: checkboxes are hidden by default so general browsing
  // stays clutter-free. Clicking the header "Delete" button toggles
  // this on, revealing the checkbox column + Select All row + bulk
  // action bar. Exits automatically after a successful bulk action,
  // on Cancel, or on tab change.
  const [isDeleteMode, setIsDeleteMode] = useState(false);
  const [page, setPage] = useState(initialUrlState.page);
  const [totalPages, setTotalPages] = useState(1);
  const [totalItems, setTotalItems] = useState(0);
  const [statusCounts, setStatusCounts] = useState<Record<string, number>>({});
  // ── Inline edit state ─────────────────────────────────────────────
  // Set `editingId` to the id of the item currently being edited. The
  // shape of `editDraft` depends on the item type:
  //   content/blog/ad -> { title, content }
  //   social_post     -> { title, posts: [{platform, text, hashtags}] }
  // Null when no edit is in progress. Saving calls inbox.updateItem
  // with whatever subset of the draft is populated.
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<any>(null);
  const [editSaving, setEditSaving] = useState(false);
  // Short-lived "highlight" id — when an inbox row is landed on via a
  // notification (URL ?id=...), we pulse its background briefly so the
  // user can see exactly which item the notification was about.
  // Cleared automatically after ~1.8s.
  const [highlightedId, setHighlightedId] = useState<string | null>(null);
  const PAGE_SIZE = 20;
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  const fetchCounts = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await inbox.counts(tenantId);
      setStatusCounts(data.counts || {});
    } catch {}
  }, [tenantId]);

  const fetchItems = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await inbox.list(tenantId, activeTab, page, PAGE_SIZE);
      setItems(data.items || []);
      setTotalPages(data.total_pages || 1);
      setTotalItems(data.total || 0);
      fetchCounts();
    } catch {
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, [tenantId, activeTab, page, fetchCounts]);

  useEffect(() => {
    fetchItems();
  }, [fetchItems]);

  // Reactive URL → selected-item sync. Uses useSearchParams so the
  // effect fires not only on mount (initial load) but also when
  // another component navigates here via router.push("/inbox?id=X")
  // — the notification bell click-handler does exactly that. Without
  // this, a user already on /inbox who clicks a notification would
  // see the URL change but the selection wouldn't update.
  //
  // IMPORTANT: only depends on `urlItemId`. Adding `items` to deps
  // caused a flicker — every socket refetch invalidated `items` which
  // re-fired this effect, which (even with the bail guard) can
  // cascade into the state→URL sync below and re-trigger renders.
  // Using a ref for items lets the effect read the latest list
  // without subscribing to changes.
  const searchParams = useSearchParams();
  const urlItemId = searchParams?.get("id") || "";
  const itemsRef = useRef<InboxItem[]>([]);
  useEffect(() => { itemsRef.current = items; }, [items]);

  // Detail-pane scroll reset. The right-hand detail pane has its own
  // scrollbar; when the user clicks a different item, the reader would
  // otherwise stay at the previous item's scroll position and the new
  // item's header / action buttons would be below the fold. Walk the
  // pane for the first `overflow-auto` descendant and reset its scroll
  // top. Smooth behavior keeps it from feeling abrupt.
  const detailPaneRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!selected?.id || !detailPaneRef.current) return;
    const scroller = detailPaneRef.current.querySelector<HTMLElement>(
      "[data-inbox-scroll='true'], .overflow-auto",
    );
    if (scroller) {
      scroller.scrollTo({ top: 0, behavior: "smooth" });
    }
  }, [selected?.id]);
  // Tracks the last `urlItemId` we successfully resolved (or attempted
  // and toasted as missing). Two purposes:
  //   1. Prevents the resolution from re-running every time the items
  //      array gets a new reference (socket refetches) — items.length
  //      is stable but the dep still re-fires when an item is added or
  //      removed; without this guard the toast would re-fire too.
  //   2. Lets us add `items.length` to the dep array safely so the
  //      effect re-runs when items load for the FIRST time — fixing
  //      the cold-start deep-link race where router.push("/inbox?id=X")
  //      from another page arrives before fetchItems() resolves.
  const lastResolvedUrlIdRef = useRef<string | null>(null);

  useEffect(() => {
    // Reset the resolution tracker whenever the URL id itself changes
    // so re-navigating to the same id from a different surface
    // re-resolves rather than no-op'ing.
    if (lastResolvedUrlIdRef.current !== urlItemId) {
      lastResolvedUrlIdRef.current = null;
    }
    if (!urlItemId) return;
    if (lastResolvedUrlIdRef.current === urlItemId) return;
    // Wait for the first items fetch to land. Adding `items.length` to
    // deps (instead of `items`) means we re-fire only when the count
    // changes — typically once on cold start (0 → N). Subsequent
    // socket refetches that don't change the count don't re-fire.
    if (items.length === 0) return;

    // Deferred to next tick so we read the latest `selected` without
    // stale-closure surprises and let the state→URL sync settle first.
    const t = setTimeout(() => {
      let toastedMissing = false;
      let resolved = false;
      setSelected((prev) => {
        if (prev?.id === urlItemId) {
          resolved = true;
          return prev;
        }
        const found = itemsRef.current.find((i) => i.id === urlItemId);
        if (!found) {
          toastedMissing = true;
          return prev;
        }
        const idx = itemsRef.current.findIndex((i) => i.id === found.id);
        if (idx >= 0) setKeyboardIndex(idx);
        setMobileShowDetail(true);
        // Brief highlight so the user sees which row the deep link
        // referenced. The separate scroll-into-view effect (below)
        // picks this up and pulls the row into the viewport.
        setHighlightedId(found.id);
        resolved = true;
        return found;
      });
      // Mark as resolved (or attempted) so we don't re-run on every
      // subsequent items.length change. Setting this in both the
      // success and the toast paths keeps stale ids from re-toasting.
      if (resolved || toastedMissing) {
        lastResolvedUrlIdRef.current = urlItemId;
      }
      if (toastedMissing) {
        showToast({
          title: "This item is no longer available",
          body: "It may have been deleted, cancelled, or moved out of your current view.",
          variant: "warning",
        });
      }
    }, 0);
    return () => clearTimeout(t);
  }, [urlItemId, items.length]);

  // Clear the highlight after 1.8s so it pulses briefly then fades.
  // Also scroll the highlighted row into view so users landing on a
  // long inbox via a notification see it without manual scrolling.
  useEffect(() => {
    if (!highlightedId) return;
    requestAnimationFrame(() => {
      const el = document.querySelector(`[data-inbox-item="${highlightedId}"]`);
      if (el && typeof (el as any).scrollIntoView === "function") {
        (el as HTMLElement).scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
    const t = setTimeout(() => setHighlightedId(null), 1800);
    return () => clearTimeout(t);
  }, [highlightedId]);

  // Sync URL query params whenever tab / page / selected id change.
  // Uses replaceState (not pushState) so back-button doesn't get
  // polluted with every selection change.
  useEffect(() => {
    if (typeof window === "undefined") return;
    const sp = new URLSearchParams();
    if (activeTab) sp.set("tab", activeTab);
    if (page > 1) sp.set("page", String(page));
    if (selected?.id) sp.set("id", selected.id);
    const newUrl = `${window.location.pathname}${sp.toString() ? "?" + sp.toString() : ""}`;
    window.history.replaceState({}, "", newUrl);
  }, [activeTab, page, selected?.id]);

  // Keyboard shortcuts -- power-user inbox navigation:
  //   j / ↓     -- next item
  //   k / ↑     -- previous item
  //   Enter     -- open the focused item
  //   e         -- mark complete (archive)
  //   a         -- approve & send (email drafts only)
  //   Escape    -- close detail pane / unselect
  // Listener is gated on inputs/contenteditable not being focused so
  // typing in the chat or editor doesn't accidentally trigger actions.
  useEffect(() => {
    function isTypingTarget(e: KeyboardEvent): boolean {
      const t = e.target as HTMLElement | null;
      if (!t) return false;
      const tag = t.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (t.isContentEditable) return true;
      return false;
    }
    function onKey(e: KeyboardEvent) {
      if (isTypingTarget(e)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;

      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        setKeyboardIndex((i) => Math.min(items.length - 1, i + 1));
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        setKeyboardIndex((i) => Math.max(0, i - 1));
      } else if (e.key === "Enter") {
        const item = items[keyboardIndex];
        if (item) {
          e.preventDefault();
          setSelected(item);
          setMobileShowDetail(true);
        }
      } else if (e.key === "Escape") {
        if (selected) {
          e.preventDefault();
          setSelected(null);
          setMobileShowDetail(false);
        }
      } else if (e.key === "e" && selected) {
        e.preventDefault();
        handleStatusChange(selected, "completed");
      } else if (e.key === "a" && selected && selected.email_draft && selected.status === "draft_pending_approval") {
        e.preventDefault();
        handleApproveSend(selected);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [items, keyboardIndex, selected]);

  // Listen for real-time inbox events via Socket.IO
  useEffect(() => {
    if (!tenantId) return;
    let socket: any = null;
    try {
      const { getSocket } = require("@/lib/socket");
      socket = getSocket();
      const handler = () => { fetchItems(); };
      socket.on("inbox_new_item", handler);
      socket.on("inbox_item_updated", handler);
      return () => { socket.off("inbox_new_item", handler); socket.off("inbox_item_updated", handler); };
    } catch {
      // socket lib may not be available
    }
  }, [tenantId, fetchItems]);

  const handleStatusChange = async (item: InboxItem, newStatus: string) => {
    try {
      await inbox.update(item.id, { status: newStatus });
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, status: newStatus } : i)));
      if (selected?.id === item.id) setSelected({ ...item, status: newStatus });
      showToast({ title: `Marked as ${newStatus.replace(/_/g, " ")}`, variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't update status",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  };

  const handleDelete = async (item: InboxItem) => {
    // Soft-delete by default: the backend flips status to "cancelled"
    // and keeps the row so the user can restore it from the Cancelled
    // tab. "Delete forever" is a separate explicit action.
    const ok = await confirm({
      title: "Cancel this item?",
      message: `"${item.title.slice(0, 80)}" will move to the Cancelled tab. You can restore it later.`,
      confirmLabel: "Cancel item",
      cancelLabel: "Keep",
      destructive: true,
    });
    if (!ok) return;
    try {
      await inbox.remove(item.id);
      setItems((prev) => prev.filter((i) => i.id !== item.id));
      if (selected?.id === item.id) {
        setSelected(null);
        setMobileShowDetail(false);
      }
      showToast({ title: "Moved to Cancelled", body: "Find it in the Cancelled tab.", variant: "success" });
      fetchCounts();
    } catch (err: any) {
      showToast({
        title: "Couldn't cancel item",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  };

  const handleRestore = async (item: InboxItem) => {
    try {
      const res = await authFetch(`${API_URL}/api/inbox/${item.id}/restore`, { method: "POST" });
      if (!res.ok) throw new Error(`restore failed (${res.status})`);
      const data = await res.json();
      setItems((prev) => prev.filter((i) => i.id !== item.id));
      if (selected?.id === item.id) {
        setSelected(null);
        setMobileShowDetail(false);
      }
      showToast({
        title: "Restored",
        body: `Moved back to "${(data?.status || "needs review").replace(/_/g, " ")}".`,
        variant: "success",
      });
      fetchCounts();
    } catch (err: any) {
      showToast({
        title: "Couldn't restore item",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  };

  const handleDeletePermanently = async (item: InboxItem) => {
    const ok = await confirm({
      title: "Delete forever?",
      message: `"${item.title.slice(0, 80)}" will be permanently removed from the database. This can't be undone.`,
      confirmLabel: "Delete forever",
      cancelLabel: "Keep",
      destructive: true,
    });
    if (!ok) return;
    try {
      const res = await authFetch(`${API_URL}/api/inbox/${item.id}?permanent=true`, { method: "DELETE" });
      if (!res.ok) throw new Error(`delete failed (${res.status})`);
      setItems((prev) => prev.filter((i) => i.id !== item.id));
      if (selected?.id === item.id) {
        setSelected(null);
        setMobileShowDetail(false);
      }
      showToast({ title: "Deleted forever", variant: "success" });
      fetchCounts();
    } catch (err: any) {
      showToast({
        title: "Couldn't delete",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  };

  const handleCancelProcessing = async (item: InboxItem, e: React.MouseEvent) => {
    e.stopPropagation();
    try {
      await inbox.remove(item.id);
      setItems((prev) => prev.filter((i) => i.id !== item.id));
      if (selected?.id === item.id) {
        setSelected(null);
        setMobileShowDetail(false);
      }
      showToast({ title: "Cancelled", variant: "info" });
    } catch (err: any) {
      showToast({ title: "Couldn't cancel", body: err?.message || "Network error.", variant: "error" });
    }
  };

  const handleDownloadImage = async (item: InboxItem) => {
    const match = (item.content || "").match(/!\[[^\]]*\]\((https?:\/\/[^\s)]+)\)/);
    const url = match ? match[1] : (item.content || "").match(/https?:\/\/\S+\.(?:png|jpg|jpeg|webp|gif)/i)?.[0];
    if (!url) {
      showToast({ title: "No image found", body: "Could not extract an image URL from this item.", variant: "error" });
      return;
    }
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`Fetch failed (${res.status})`);
      const blob = await res.blob();
      const ext = (url.split(".").pop() || "png").split("?")[0].toLowerCase();
      const safeTitle = (item.title || "image").replace(/[^a-z0-9-_]+/gi, "_").slice(0, 60);
      const filename = `${safeTitle || "image"}.${ext}`;
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(objectUrl);
    } catch (err: any) {
      showToast({ title: "Download failed", body: err?.message || "Could not download the image.", variant: "error" });
    }
  };

  const handleCopy = (content: string) => {
    navigator.clipboard.writeText(content);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleApproveSend = async (item: InboxItem) => {
    if (!tenantId || actionLoading) return;
    setActionLoading("approve");
    try {
      await inbox.approveSend(tenantId, item.id);
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, status: "sent" } : i)));
      if (selected?.id === item.id) setSelected({ ...item, status: "sent" });
      showToast({
        title: "Email sent",
        body: item.email_draft?.to ? `Delivered to ${item.email_draft.to}` : undefined,
        variant: "success",
      });
    } catch (err: any) {
      showToast({
        title: "Couldn't send email",
        body: err?.message || "Check Gmail connection in Settings.",
        variant: "error",
        href: "/settings",
      });
    } finally {
      setActionLoading(null);
    }
  };

  const handleCancelDraft = async (item: InboxItem) => {
    if (!tenantId || actionLoading) return;
    // Optional feedback loop: ask WHY the draft was rejected so the
    // reason can be replayed into future agent prompts via
    // summarize_cancel_reasons_for_prompt. Blank / cancelled prompt
    // still cancels the draft — the reason is strictly opt-in.
    let reason = "";
    if (typeof window !== "undefined") {
      const entered = window.prompt(
        "Optional: why are you cancelling this draft?\n(Leave blank to just cancel. Your reason helps the agent avoid the same mistake next time.)",
        "",
      );
      // null → user hit Cancel on the prompt; still proceed with the cancel.
      reason = (entered || "").trim();
    }
    setActionLoading("cancel");
    try {
      await inbox.cancelDraft(tenantId, item.id, reason);
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, status: "cancelled" } : i)));
      if (selected?.id === item.id) setSelected({ ...item, status: "cancelled" });
      showToast({ title: reason ? "Draft cancelled — feedback saved" : "Draft cancelled", variant: "info" });
    } catch (err: any) {
      showToast({
        title: "Couldn't cancel draft",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
    setActionLoading(null);
  };

  const handlePublishSocial = async (item: InboxItem) => {
    if (!tenantId || actionLoading) return;
    setActionLoading("publish");
    try {
      const res = await inbox.approvePublishSocial(tenantId, item.id);
      const newStatus = res.status === "sent" ? "sent" : "failed";
      setItems((prev) => prev.map((i) => (i.id === item.id ? { ...i, status: newStatus } : i)));
      if (selected?.id === item.id) setSelected({ ...item, status: newStatus });
      if (newStatus === "failed") {
        showToast({
          title: "Couldn't publish to X",
          body: "Check your Twitter connection in Settings.",
          variant: "error",
          href: "/settings",
        });
      } else {
        showToast({ title: "Published to X", variant: "success" });
      }
    } catch (err: any) {
      showToast({
        title: "Couldn't publish to X",
        body: err?.message || "Check connection in Settings.",
        variant: "error",
        href: "/settings",
      });
    } finally {
      setActionLoading(null);
    }
  };

  const handlePublishLinkedIn = async (item: InboxItem) => {
    if (!tenantId || actionLoading) return;
    // Confirm BEFORE setting actionLoading so the spinner doesn't appear
    // while the modal is up (was setting actionLoading then native
    // confirm() which felt like the page froze).
    const ok = await confirm({
      title: "Publish to LinkedIn?",
      message: "This will be visible publicly and cannot be undone.",
      confirmLabel: "Publish",
      cancelLabel: "Cancel",
    });
    if (!ok) return;
    setActionLoading("linkedin");
    try {
      // Extract LinkedIn-specific text from social post content
      const posts = parseSocialPosts(item.content);
      let text = "";
      let imageUrl = "";
      if (posts.length > 0) {
        // Prefer the LinkedIn post; fall back to first post
        const post = posts.find(p => p.platform?.toLowerCase() === "linkedin") || posts[0];
        text = post.text || "";
        imageUrl = (post as any).image_url || "";
        const hashtags = post.hashtags || [];
        if (hashtags.length > 0) {
          const tagStr = hashtags.map((t: string) => `#${t.replace(/^#/, "")}`).join(" ");
          if (!text.includes(tagStr)) text = `${text}\n\n${tagStr}`;
        }
      } else {
        // Fallback path — strip agent meta-commentary (inbox item id,
        // "Status: ...", "**Post summary:**", "X post for Y saved to
        // ARIA inbox...") before handing to the backend. Backend also
        // sanitizes as a belt-and-suspenders check, but doing it here
        // means we can bail cleanly with a clear toast instead of a
        // 400.
        text = (item.content || "")
          .replace(/\(\s*item\s+[a-f0-9-]{6,}\s*\)/gi, "")
          .replace(/^\s*(linkedin post|twitter post|tweet|x post|social post|post)s?\s+for\s+[^.\n]*\s+(created|saved|ready|generated)[^.\n]*\.?$/gim, "")
          .replace(/^\s*status\s*:\s*[a-z_]+\s*\.?$/gim, "")
          .replace(/\*\*\s*post\s+summary\s*:?\s*\*\*/gi, "")
          .replace(/^\s*(saved to aria inbox|successfully saved|draft saved|draft id:).*$/gim, "")
          .replace(/^\s*#{1,3}\s+(done|task complete|result|summary).*$/gim, "")
          .replace(/\n{3,}/g, "\n\n")
          .trim();
        if (!text || text.length < 20) {
          showToast({
            title: "Nothing to publish",
            body: "This row looks like an agent summary, not a real post. Ask the CEO to regenerate the post.",
            variant: "error",
          });
          return;
        }
      }

      // Fall back to the row-level thumbnail (metadata.image_url /
      // markdown / raw URL) if no per-post image_url was set — covers
      // older rows and the degraded-parse path.
      if (!imageUrl) {
        const thumb = getInboxThumbnail(item);
        if (thumb) imageUrl = thumb;
      }
      const res = await authFetch(`${API_URL}/api/linkedin/${tenantId}/post`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, image_url: imageUrl || undefined, confirmed: true }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Failed (${res.status})`);
      }
      showToast({ title: "Published to LinkedIn", variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't publish to LinkedIn",
        body: err?.message || "Check connection in Settings.",
        variant: "error",
        href: "/settings",
      });
    } finally {
      setActionLoading(null);
    }
  };

  // ─── Schedule Picker ──
  const [scheduleItem, setScheduleItem] = useState<InboxItem | null>(null);
  const [scheduleDate, setScheduleDate] = useState("");
  const [scheduleTime, setScheduleTime] = useState("09:00");
  const [schedulePlatform, setSchedulePlatform] = useState("");
  const [scheduling, setScheduling] = useState(false);

  const handleSchedule = async () => {
    if (!tenantId || !scheduleItem || !scheduleDate || !scheduleTime) return;
    setScheduling(true);
    try {
      const scheduledAt = new Date(`${scheduleDate}T${scheduleTime}:00`).toISOString();
      const isEmail = scheduleItem.type === "email" || scheduleItem.type === "email_sequence" || !!scheduleItem.email_draft;
      const isSocial = scheduleItem.type === "social_post" || scheduleItem.type === "social";

      // Pick task_type from item type. Email/social have dedicated executors;
      // everything else (blog_post, ad_campaign, general, follow_up) becomes
      // a generic 'reminder' task -- it shows up on the calendar as a
      // "publish this" reminder, fires a notification at the scheduled time,
      // and links back to the inbox item via inbox_item_id so the user can
      // jump straight to it.
      let taskType: string;
      if (isEmail) taskType = "send_email";
      else if (isSocial) taskType = "publish_post";
      else taskType = "reminder";

      const title = scheduleItem.title || (isEmail ? "Scheduled email" : isSocial ? "Scheduled post" : "Scheduled task");

      let payload: Record<string, any> = { inbox_item_id: scheduleItem.id };
      if (isEmail && scheduleItem.email_draft) {
        payload = {
          ...payload,
          to: scheduleItem.email_draft.to,
          subject: scheduleItem.email_draft.subject,
          html_body: scheduleItem.email_draft.html_body,
        };
      } else if (isSocial) {
        const posts = parseSocialPosts(scheduleItem.content);
        const post = schedulePlatform === "linkedin"
          ? posts.find(p => p.platform?.toLowerCase() === "linkedin") || posts[0]
          : posts.find(p => p.platform?.toLowerCase() === "twitter") || posts[0];
        if (post) payload = { ...payload, text: post.text, platform: schedulePlatform || post.platform || "twitter" };
      } else {
        // Generic reminder for blog_post / ad_campaign / general -- the
        // executor's _execute_reminder writes a notification at the
        // scheduled time. The body links back to the inbox item.
        payload = {
          ...payload,
          title: scheduleItem.title,
          body: `Time to publish: ${scheduleItem.title}. Open your inbox to review and ship.`,
          description: scheduleItem.content?.slice(0, 500) || "",
        };
      }

      await authFetch(`${API_URL}/api/schedule/${tenantId}/tasks`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_type: taskType, title, scheduled_at: scheduledAt, payload, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone }),
      });
      showToast({
        title: "Scheduled",
        body: `Will fire on ${new Date(scheduledAt).toLocaleString()}. Approve from the calendar before then.`,
        variant: "success",
        href: "/calendar",
      });
      setScheduleItem(null);
      setScheduleDate("");
      setScheduleTime("09:00");
    } catch (err: any) {
      showToast({
        title: "Couldn't schedule",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    } finally {
      setScheduling(false);
    }
  };

  const [waReplyText, setWaReplyText] = useState("");
  const [waReplying, setWaReplying] = useState(false);

  const handleWhatsAppReply = async (item: InboxItem) => {
    if (!tenantId || !waReplyText.trim()) return;
    // Parse from_number from item metadata stored in content title
    const fromMatch = item.title.match(/\+?\d{10,15}/);
    const toNumber = fromMatch?.[0] || "";
    if (!toNumber) {
      showToast({ title: "Cannot determine recipient number", variant: "error" });
      return;
    }
    const ok = await confirm({
      title: "Send WhatsApp message?",
      message: `Send to ${toNumber}? This cannot be undone.`,
      confirmLabel: "Send",
      cancelLabel: "Cancel",
    });
    if (!ok) return;
    setWaReplying(true);
    try {
      const res = await authFetch(`${API_URL}/api/whatsapp/${tenantId}/send?confirmed=true`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ to: toNumber, message: waReplyText }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Send failed (${res.status})`);
      }
      setWaReplyText("");
      showToast({ title: "WhatsApp reply sent", variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't send WhatsApp reply",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    } finally {
      setWaReplying(false);
    }
  };

  const isEmailDraft = (item: InboxItem) => !!item.email_draft;
  // Only route to the social-post view for ACTUAL social_manager
  // rows. Other agents (Media Designer, etc.) sometimes land with
  // type="social_post" when they've been mis-labelled or when the
  // merge-window dedup ran too liberally — routing those to the
  // platform-card UI would trigger the "needs regenerating" panel
  // on rows that never had posts to begin with. Let the standard
  // detail view render them instead.
  const isSocialPost = (item: InboxItem) =>
    item.type === "social_post" && item.agent === "social_manager";
  const isPendingApproval = (item: InboxItem) => item.status === "draft_pending_approval";

  const filteredItems = items;

  // ─── Bulk actions ───
  const toggleCheck = (id: string) => {
    setCheckedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAllChecked = () => {
    if (checkedIds.size === filteredItems.length) {
      setCheckedIds(new Set());
    } else {
      setCheckedIds(new Set(filteredItems.map((i) => i.id)));
    }
  };

  const handleBulkDelete = async () => {
    if (checkedIds.size === 0) return;
    const count = checkedIds.size;
    const ok = await confirm({
      title: `Delete ${count} item${count === 1 ? "" : "s"}?`,
      message: "These items will be permanently removed.",
      confirmLabel: "Delete all",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    setActionLoading("bulk-delete");
    try {
      await Promise.all(Array.from(checkedIds).map((id) => inbox.remove(id)));
      setItems((prev) => prev.filter((i) => !checkedIds.has(i.id)));
      if (selected && checkedIds.has(selected.id)) setSelected(null);
      setCheckedIds(new Set());
      showToast({ title: `Deleted ${count} item${count === 1 ? "" : "s"}`, variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Some deletions failed",
        body: err?.message || "Refresh to see what was deleted.",
        variant: "error",
      });
    }
    setActionLoading(null);
  };

  const handleBulkComplete = async () => {
    if (checkedIds.size === 0) return;
    const count = checkedIds.size;
    setActionLoading("bulk-complete");
    try {
      await Promise.all(Array.from(checkedIds).map((id) => inbox.update(id, { status: "completed" })));
      setItems((prev) =>
        prev.map((i) => (checkedIds.has(i.id) ? { ...i, status: "completed" } : i))
      );
      if (selected && checkedIds.has(selected.id)) setSelected({ ...selected, status: "completed" });
      setCheckedIds(new Set());
      showToast({ title: `Marked ${count} item${count === 1 ? "" : "s"} complete`, variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Some updates failed",
        body: err?.message || "Refresh to see current state.",
        variant: "error",
      });
    }
    setActionLoading(null);
  };

  // ─── Save draft edits ───
  const handleSaveDraft = async (item: InboxItem, data: { to: string; subject: string; html_body: string }) => {
    if (!tenantId) return;
    try {
      const result = await inbox.updateDraft(tenantId, item.id, data);
      // Update local state with saved draft
      const updatedDraft = result.email_draft || { ...item.email_draft, ...data };
      setItems((prev) =>
        prev.map((i) => (i.id === item.id ? { ...i, email_draft: updatedDraft } : i))
      );
      if (selected?.id === item.id) {
        setSelected({ ...item, email_draft: updatedDraft });
      }
      showToast({ title: "Draft saved", variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't save draft",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
      throw err;
    }
  };

  // ─── Inline edit helpers (content + social) ──────────────────────
  // Generic edit for non-email inbox items. Emails already have their
  // own EmailEditor component with approve/schedule/send UX — these
  // helpers cover the gap for Content Writer / Ad Strategist markdown
  // outputs and Social Manager per-platform posts.
  const beginEditContent = (item: InboxItem) => {
    setEditingId(item.id);
    setEditDraft({ title: item.title, content: item.content });
  };
  const beginEditSocial = (item: InboxItem) => {
    const posts = parseSocialPosts(item.content);
    setEditingId(item.id);
    setEditDraft({
      title: item.title,
      posts: posts.length
        ? posts.map((p) => ({
            platform: p.platform || "twitter",
            text: p.text || "",
            hashtags: p.hashtags || [],
          }))
        : [{ platform: "twitter", text: "", hashtags: [] }],
    });
  };
  const cancelEdit = () => {
    setEditingId(null);
    setEditDraft(null);
  };

  const handleSaveEdit = async (item: InboxItem) => {
    if (!editDraft || editSaving) return;
    setEditSaving(true);
    try {
      const updates: any = {};
      if (typeof editDraft.title === "string" && editDraft.title !== item.title) {
        updates.title = editDraft.title;
      }
      if (Array.isArray(editDraft.posts)) {
        // Social posts path — backend converts social_posts[] into
        // the {posts:[...]} JSON blob on the content column.
        updates.social_posts = editDraft.posts;
      } else if (typeof editDraft.content === "string" && editDraft.content !== item.content) {
        updates.content = editDraft.content;
      }
      if (Object.keys(updates).length === 0) {
        cancelEdit();
        return;
      }
      await inbox.updateItem(item.id, updates);
      // Optimistically update local state so the user sees their edit
      // applied without waiting for the socket event to echo back.
      const newContent = updates.social_posts
        ? JSON.stringify({ posts: updates.social_posts })
        : updates.content ?? item.content;
      const patched = {
        ...item,
        title: updates.title ?? item.title,
        content: newContent,
      };
      setItems((prev) => prev.map((i) => (i.id === item.id ? patched : i)));
      if (selected?.id === item.id) setSelected(patched);
      showToast({ title: "Saved", variant: "success" });
      cancelEdit();
    } catch (err: any) {
      showToast({
        title: "Couldn't save edit",
        body: err?.message || "Network error — try again.",
        variant: "error",
      });
    }
    setEditSaving(false);
  };

  // ─── Email Draft Editor (editable) ───
  const renderEmailEditor = (item: InboxItem) => {
    const draft = item.email_draft!;
    return (
      <EmailEditor
        key={item.id}
        to={draft.to || ""}
        subject={draft.subject || ""}
        htmlBody={draft.html_body || ""}
        onSave={(data) => handleSaveDraft(item, data)}
        onSend={() => handleApproveSend(item)}
        onSchedule={() => setScheduleItem(item)}
        onCancel={() => handleCancelDraft(item)}
        sendDisabled={actionLoading === "approve"}
        sendLoading={actionLoading === "approve"}
        cancelLoading={actionLoading === "cancel"}
      />
    );
  };

  // ─── Email Draft Read-Only View (sent/failed/cancelled) ───
  const renderEmailReadOnly = (item: InboxItem) => {
    const draft = item.email_draft!;
    return (
      <div className="flex flex-col w-full">
        <div className="border-b border-[#E0DED8] p-5">
          <div className="flex items-center gap-2 mb-3">
            <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }} />
            <span className="text-sm font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
              {AGENT_NAMES[item.agent] || item.agent}
            </span>
            <span className="text-xs text-[#9E9C95]">Email</span>
            <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
          </div>
          <div className="bg-[#F8F8F6] rounded-lg p-4 space-y-2 mb-3">
            <div className="flex items-baseline gap-2">
              <span className="text-xs font-semibold text-[#5F5E5A] uppercase w-16 shrink-0">To</span>
              <span className="text-sm text-[#2C2C2A]">{draft.to || "—"}</span>
            </div>
            <div className="flex items-baseline gap-2">
              <span className="text-xs font-semibold text-[#5F5E5A] uppercase w-16 shrink-0">Subject</span>
              <span className="text-sm font-medium text-[#2C2C2A]">{draft.subject}</span>
            </div>
          </div>
          {(() => {
            const badge = STATUS_BADGES[item.status];
            return badge ? (
              <span className={`inline-flex items-center text-[11px] px-2.5 py-1 rounded-full border ${badge.bg} ${badge.text} ${badge.border} font-medium`}>
                {badge.label}
              </span>
            ) : null;
          })()}
        </div>
        {/* Action bar — above content */}
        <div className="border-b border-[#E0DED8] px-5 py-3 flex items-center gap-2 bg-[#F8F8F6]">
          {item.status === "sent" && (
            <span className="flex items-center gap-1.5 text-sm font-medium text-[#1D9E75]">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              Email sent successfully
            </span>
          )}
          {item.status === "failed" && (
            <>
              <span className="flex items-center gap-1.5 text-sm font-medium text-red-500">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m9-.75a9 9 0 11-18 0 9 9 0 0118 0zm-9 3.75h.008v.008H12v-.008z" />
                </svg>
                Send failed
              </span>
              <button
                onClick={() => {
                  setItems(prev => prev.map(i => i.id === item.id ? { ...i, status: "draft_pending_approval" } : i));
                  setSelected({ ...item, status: "draft_pending_approval" });
                }}
                className="px-3 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] bg-white text-[#5F5E5A] hover:bg-[#F0EFEC] transition-colors"
              >
                Edit & Retry
              </button>
            </>
          )}
          {item.status === "cancelled" ? (
            <>
              <button onClick={() => handleRestore(item)} className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors">
                Restore
              </button>
              <button onClick={() => handleDeletePermanently(item)} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-red-300 text-red-500 hover:bg-red-50 transition-colors">
                Delete forever
              </button>
            </>
          ) : (
            <button
              onClick={() => handleDelete(item)}
              className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors"
            >
              Delete
            </button>
          )}
        </div>
        {/* Email content */}
        <div className="flex-1 overflow-auto p-5 pb-24" data-inbox-scroll="true">
          <div className="bg-white rounded-lg border border-[#E0DED8] overflow-hidden">
            <iframe
              ref={iframeRef}
              srcDoc={draft.html_body}
              title="Email preview"
              className="w-full min-h-[300px] border-0"
              sandbox="allow-same-origin"
              onLoad={() => {
                if (iframeRef.current?.contentDocument) {
                  const h = iframeRef.current.contentDocument.body.scrollHeight;
                  iframeRef.current.style.height = `${Math.max(h + 20, 300)}px`;
                }
              }}
            />
          </div>
        </div>
      </div>
    );
  };

  // ─── Parse social posts from JSON content ───
  const parseSocialPosts = (content: string): { platform: string; text: string; hashtags?: string[] }[] => {
    try {
      const start = content.indexOf("{");
      const end = content.lastIndexOf("}") + 1;
      if (start >= 0 && end > start) {
        const data = JSON.parse(content.substring(start, end));
        if (data.posts && Array.isArray(data.posts)) return data.posts;
      }
    } catch {}
    try {
      const start = content.indexOf("[");
      const end = content.lastIndexOf("]") + 1;
      if (start >= 0 && end > start) return JSON.parse(content.substring(start, end));
    } catch {}
    return [];
  };

  // ─── Social post detail view (tweet cards) ───
  const renderSocialDetail = (item: InboxItem) => {
    const isEditing = editingId === item.id;
    type SocialPost = { platform: string; text: string; hashtags?: string[] };
    const posts: SocialPost[] = isEditing && editDraft?.posts ? editDraft.posts : parseSocialPosts(item.content);
    const PLATFORM_ICONS: Record<string, React.ReactNode> = {
      twitter: <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" /></svg>,
      linkedin: <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" /></svg>,
      facebook: <svg className="w-5 h-5" viewBox="0 0 24 24" fill="currentColor"><path d="M24 12.073c0-6.627-5.373-12-12-12s-12 5.373-12 12c0 5.99 4.388 10.954 10.125 11.854v-8.385H7.078v-3.47h3.047V9.43c0-3.007 1.792-4.669 4.533-4.669 1.312 0 2.686.235 2.686.235v2.953H15.83c-1.491 0-1.956.925-1.956 1.874v2.25h3.328l-.532 3.47h-2.796v8.385C19.612 23.027 24 18.062 24 12.073z" /></svg>,
    };
    const PLATFORM_COLORS: Record<string, { bg: string; border: string; iconBg: string }> = {
      twitter: { bg: "bg-white", border: "border-gray-200", iconBg: "bg-black text-white" },
      linkedin: { bg: "bg-white", border: "border-blue-100", iconBg: "bg-[#0A66C2] text-white" },
      facebook: { bg: "bg-white", border: "border-blue-100", iconBg: "bg-[#1877F2] text-white" },
    };
    const PLATFORM_NAMES: Record<string, string> = { twitter: "X / Twitter", linkedin: "LinkedIn", facebook: "Facebook" };

    return (
      <div className="flex flex-col w-full">
        <div className="border-b border-[#E0DED8] p-5">
          <div className="flex items-center gap-2 mb-2">
            <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }} />
            <span className="text-sm font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
              {AGENT_NAMES[item.agent] || item.agent}
            </span>
            <span className="text-xs text-[#9E9C95]">Social Post</span>
            <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
          </div>
          {isEditing ? (
            <input
              value={editDraft?.title ?? ""}
              onChange={(e) => setEditDraft({ ...editDraft, title: e.target.value })}
              className="w-full text-lg font-semibold text-[#2C2C2A] bg-white border border-[#534AB7]/40 rounded-md px-2 py-1 focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30"
              placeholder="Title"
            />
          ) : (
            <h2 className="text-lg font-semibold text-[#2C2C2A]">{item.title}</h2>
          )}
          <div className="flex items-center gap-2 mt-3 flex-wrap">
            {!isEditing && (item.status === "ready" || item.status === "needs_review" || item.status === "failed") && (
              <button
                onClick={() => beginEditSocial(item)}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-white text-[#534AB7] border border-[#534AB7] hover:bg-[#EEEDFE] transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zM19.5 19.5h-15" />
                </svg>
                Edit
              </button>
            )}
            {isEditing && (
              <>
                <button
                  onClick={() => handleSaveEdit(item)}
                  disabled={editSaving}
                  className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors disabled:opacity-60"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                  </svg>
                  {editSaving ? "Saving..." : "Save"}
                </button>
                <button
                  onClick={cancelEdit}
                  disabled={editSaving}
                  className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors disabled:opacity-60"
                >
                  Cancel
                </button>
              </>
            )}
            {!isEditing && isSocialPost(item) && (item.status === "ready" || item.status === "needs_review" || item.status === "failed") && (
              <button
                onClick={() => handlePublishSocial(item)}
                disabled={actionLoading === "publish"}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-black text-white hover:bg-gray-800 transition-colors disabled:opacity-60"
              >
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
                </svg>
                {actionLoading === "publish" ? "Publishing..." : item.status === "failed" ? "Retry Publish" : "Publish to X"}
              </button>
            )}
            {!isEditing && isSocialPost(item) && (item.status === "ready" || item.status === "needs_review" || item.status === "failed") && (
              <button
                onClick={() => handlePublishLinkedIn(item)}
                disabled={actionLoading === "linkedin"}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#0A66C2] text-white hover:bg-[#084d93] transition-colors disabled:opacity-60"
              >
                <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor">
                  <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" />
                </svg>
                {actionLoading === "linkedin" ? "Publishing..." : "Publish to LinkedIn"}
              </button>
            )}
            {!isEditing && isSocialPost(item) && (item.status === "ready" || item.status === "needs_review") && (
              <button
                onClick={() => { setScheduleItem(item); setSchedulePlatform(""); }}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-white text-[#534AB7] border border-[#534AB7] hover:bg-[#EEEDFE] transition-colors"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 7.5v11.25m-18 0A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75m-18 0v-7.5A2.25 2.25 0 015.25 9h13.5A2.25 2.25 0 0121 11.25v7.5" /></svg>
                Schedule
              </button>
            )}
            {isSocialPost(item) && item.status === "sent" && (
              <span className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-emerald-50 text-emerald-700 border border-emerald-200">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
                </svg>
                Published
              </span>
            )}
            {item.status === "ready" && (
              <button onClick={() => handleStatusChange(item, "completed")} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors">
                Mark complete
              </button>
            )}
            {item.status === "cancelled" ? (
              <>
                <button onClick={() => handleRestore(item)} className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors">
                  Restore
                </button>
                <button onClick={() => handleDeletePermanently(item)} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-red-300 text-red-500 hover:bg-red-50 transition-colors">
                  Delete forever
                </button>
              </>
            ) : (
              <button onClick={() => handleDelete(item)} className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors">
                Delete
              </button>
            )}
          </div>
        </div>
        <div className="flex-1 overflow-auto p-5 pb-24 space-y-4" data-inbox-scroll="true">
          {isEditing && (
            <div className="space-y-3">
              {(editDraft?.posts || []).map((p: any, idx: number) => {
                const platform = (p.platform || "twitter").toLowerCase();
                const charLimit = platform === "twitter" ? 280 : platform === "linkedin" ? 3000 : 2000;
                const updatePost = (changes: any) => {
                  const nextPosts = [...(editDraft?.posts || [])];
                  nextPosts[idx] = { ...nextPosts[idx], ...changes };
                  setEditDraft({ ...editDraft, posts: nextPosts });
                };
                return (
                  <div key={idx} className="rounded-xl border border-[#E0DED8] bg-white overflow-hidden">
                    <div className="flex items-center gap-2 px-4 py-2 bg-[#F8F8F6] border-b border-[#E0DED8]">
                      <select
                        value={platform}
                        onChange={(e) => updatePost({ platform: e.target.value })}
                        className="text-sm font-medium bg-transparent border-0 focus:outline-none"
                      >
                        <option value="twitter">X / Twitter</option>
                        <option value="linkedin">LinkedIn</option>
                        <option value="facebook">Facebook</option>
                      </select>
                      <span className="text-xs text-[#9E9C95] ml-auto">
                        {(p.text || "").length}/{charLimit}
                        {platform === "twitter" && (p.text || "").length > 280 && (
                          <span className="text-red-500 ml-1 font-medium">over limit</span>
                        )}
                      </span>
                      {(editDraft?.posts?.length || 0) > 1 && (
                        <button
                          onClick={() => {
                            const nextPosts = [...(editDraft?.posts || [])];
                            nextPosts.splice(idx, 1);
                            setEditDraft({ ...editDraft, posts: nextPosts });
                          }}
                          className="text-xs text-red-500 hover:text-red-600"
                          title="Remove this post"
                        >
                          Remove
                        </button>
                      )}
                    </div>
                    <textarea
                      value={p.text || ""}
                      onChange={(e) => updatePost({ text: e.target.value })}
                      rows={5}
                      placeholder="Post text..."
                      className="w-full px-4 py-3 border-0 focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 text-[15px] text-[#0F1419] resize-y"
                    />
                    <div className="px-4 py-2 border-t border-[#E0DED8]">
                      <input
                        value={(p.hashtags || []).join(", ")}
                        onChange={(e) =>
                          updatePost({
                            hashtags: e.target.value
                              .split(",")
                              .map((t: string) => t.trim().replace(/^#/, ""))
                              .filter(Boolean),
                          })
                        }
                        placeholder="Hashtags (comma-separated, no #)"
                        className="w-full text-sm bg-transparent border-0 focus:outline-none placeholder:text-[#B0AFA8]"
                      />
                    </div>
                  </div>
                );
              })}
              <button
                onClick={() =>
                  setEditDraft({
                    ...editDraft,
                    posts: [
                      ...(editDraft?.posts || []),
                      { platform: "twitter", text: "", hashtags: [] },
                    ],
                  })
                }
                className="w-full py-2 text-sm font-medium rounded-lg border border-dashed border-[#534AB7]/40 text-[#534AB7] hover:bg-[#EEEDFE] transition-colors"
              >
                + Add post
              </button>
            </div>
          )}
          {!isEditing && posts.length > 0 ? posts.map((post, idx) => {
            const platform = (post.platform || "twitter").toLowerCase();
            const colors = PLATFORM_COLORS[platform] || PLATFORM_COLORS.twitter;
            const hashtags = post.hashtags || [];
            const charLimit = platform === "twitter" ? 280 : platform === "linkedin" ? 3000 : 2000;
            const textWithTags = hashtags.length > 0
              ? `${post.text}${post.text.includes("#") ? "" : "\n" + hashtags.map(t => `#${t.replace(/^#/, "")}`).join(" ")}`
              : post.text;

            return (
              <div key={idx} className={`rounded-xl border ${colors.border} ${colors.bg} overflow-hidden shadow-sm`}>
                {/* Platform header */}
                <div className="flex items-center gap-3 px-4 py-3 border-b border-gray-100 bg-gray-50/50">
                  <div className={`w-8 h-8 rounded-full flex items-center justify-center ${colors.iconBg}`}>
                    {PLATFORM_ICONS[platform] || PLATFORM_ICONS.twitter}
                  </div>
                  <div>
                    <span className="text-sm font-semibold text-[#2C2C2A]">{PLATFORM_NAMES[platform] || platform}</span>
                    <span className="text-xs text-[#9E9C95] ml-2">{post.text.length}/{charLimit} chars</span>
                    {platform === "twitter" && post.text.length > 280 && (
                      <span className="text-xs text-red-500 ml-1 font-medium">Over limit!</span>
                    )}
                  </div>
                  <button
                    onClick={() => { navigator.clipboard.writeText(textWithTags); setCopied(true); setTimeout(() => setCopied(false), 2000); }}
                    className="ml-auto p-1.5 rounded-lg hover:bg-gray-100 text-[#9E9C95] hover:text-[#2C2C2A] transition-colors"
                    title="Copy post"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
                    </svg>
                  </button>
                </div>
                {/* Post content */}
                <div className="px-4 py-4">
                  <p className="text-[15px] text-[#0F1419] leading-relaxed whitespace-pre-wrap">{post.text}</p>
                  {hashtags.length > 0 && !post.text.includes("#") && (
                    <div className="flex flex-wrap gap-1.5 mt-3">
                      {hashtags.map((tag, i) => (
                        <span key={i} className="text-sm text-[#1d9bf0] font-medium">
                          #{tag.replace(/^#/, "")}
                        </span>
                      ))}
                    </div>
                  )}
                  {(post as any).image_url && (
                    <div className="mt-3 rounded-xl overflow-hidden border border-gray-200 bg-gray-50">
                      <img
                        src={(post as any).image_url}
                        alt="Attached media"
                        className="w-full h-auto object-cover max-h-[360px]"
                        loading="lazy"
                      />
                    </div>
                  )}
                </div>
                {/* Footer with engagement placeholders */}
                <div className="flex items-center gap-8 px-4 py-2.5 border-t border-gray-100 text-[#536471]">
                  <span className="flex items-center gap-1.5 text-xs">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 20.25c4.97 0 9-3.694 9-8.25s-4.03-8.25-9-8.25S3 7.444 3 12c0 2.104.859 4.023 2.273 5.48.432.447.74 1.04.586 1.641a4.483 4.483 0 01-.923 1.785A5.969 5.969 0 006 21c1.282 0 2.47-.402 3.445-1.087.81.22 1.668.337 2.555.337z" /></svg>
                    Reply
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M19.5 12c0-1.232-.046-2.453-.138-3.662a4.006 4.006 0 00-3.7-3.7 48.678 48.678 0 00-7.324 0 4.006 4.006 0 00-3.7 3.7c-.017.22-.032.441-.046.662M19.5 12l3-3m-3 3l-3-3m-12 3c0 1.232.046 2.453.138 3.662a4.006 4.006 0 003.7 3.7 48.656 48.656 0 007.324 0 4.006 4.006 0 003.7-3.7c.017-.22.032-.441.046-.662M4.5 12l3 3m-3-3l-3 3" /></svg>
                    Repost
                  </span>
                  <span className="flex items-center gap-1.5 text-xs">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M21 8.25c0-2.485-2.099-4.5-4.688-4.5-1.935 0-3.597 1.126-4.312 2.733-.715-1.607-2.377-2.733-4.313-2.733C5.1 3.75 3 5.765 3 8.25c0 7.22 9 12 9 12s9-4.78 9-12z" /></svg>
                    Like
                  </span>
                </div>
              </div>
            );
          }) : !isEditing ? (
            <div className="space-y-3">
              {/* Image thumbnail fallback — even when posts can't be
                  parsed, surface the attached media so the user sees
                  what the Media Designer produced. Walks the same
                  sources parseSocialPosts would (metadata.image_url,
                  posts[].image_url, markdown, raw URL). */}
              {(() => {
                const fallbackThumb = getInboxThumbnail(item);
                if (!fallbackThumb) return null;
                return (
                  <div className="rounded-xl border border-[#E0DED8] overflow-hidden bg-white">
                    <img
                      src={fallbackThumb}
                      alt="Attached media"
                      className="w-full h-auto object-cover max-h-[360px]"
                      loading="lazy"
                    />
                  </div>
                );
              })()}
              {/* Needs-regeneration panel — this row is typed as
                  social_post but the agent returned a description
                  instead of parseable JSON posts. Show a clear CTA
                  to regenerate instead of dumping the raw summary. */}
              <div className="rounded-xl border-2 border-[#D85A30]/30 bg-gradient-to-r from-[#FDEEE8] to-[#FFFCFA] p-4">
                <div className="flex items-start gap-3">
                  <div className="w-10 h-10 rounded-full bg-[#D85A30]/15 flex items-center justify-center flex-shrink-0">
                    <svg className="w-5 h-5 text-[#D85A30]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m0 3.75h.008v-.008H12v.008zm0-12a9 9 0 100 18 9 9 0 000-18z" />
                    </svg>
                  </div>
                  <div className="flex-1 min-w-0">
                    <h4 className="text-sm font-semibold text-[#2C2C2A]">This social post needs regenerating</h4>
                    <p className="text-sm text-[#5F5E5A] mt-1 leading-relaxed">
                      The Social Manager returned a summary instead of actual post text. No platform cards to render. Ask the CEO to regenerate the post — the agent should produce a tweet and a LinkedIn post you can publish directly.
                    </p>
                    <button
                      onClick={() => {
                        const message = `Regenerate the social posts for this task: "${item.title}". Make sure to write the actual tweet text and LinkedIn post text — not a description of what they would be.`;
                        try {
                          navigator.clipboard.writeText(message);
                          showToast({ title: "Regenerate prompt copied", body: "Paste it into the CEO chat.", variant: "success" });
                        } catch {
                          showToast({ title: "Couldn't copy", body: message, variant: "info" });
                        }
                      }}
                      className="mt-3 flex items-center gap-2 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#D85A30] text-white hover:bg-[#B8491F] transition-colors"
                    >
                      <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M16.023 9.348h4.992v-.001M2.985 19.644v-4.992m0 0h4.992m-4.993 0l3.181 3.183a8.25 8.25 0 0013.803-3.7M4.031 9.865a8.25 8.25 0 0113.803-3.7l3.181 3.182m0-4.991v4.99" />
                      </svg>
                      Copy regenerate prompt
                    </button>
                  </div>
                </div>
              </div>
              {/* Still show the raw agent output below, collapsed, so
                  the user can see what the agent actually wrote. */}
              <details className="rounded-lg border border-[#E0DED8] bg-[#F8F8F6]">
                <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-[#5F5E5A] hover:bg-white transition-colors">
                  Show what the agent wrote instead
                </summary>
                <div className="px-3 pb-3 pt-1 prose prose-sm max-w-none text-[#2C2C2A]">
                  {renderInlineMarkdown(item.content)}
                </div>
              </details>
            </div>
          ) : null}
        </div>
      </div>
    );
  };

  // ─── Standard (non-email) detail view ───
  const renderWhatsAppDetail = (item: InboxItem) => (
    <div className="flex flex-col w-full">
      <div className="border-b border-[#E0DED8] p-5">
        <div className="flex items-center gap-2 mb-2">
          <div className="w-6 h-6 rounded-full bg-[#25D366] flex items-center justify-center">
            <svg className="w-3.5 h-3.5 text-white" viewBox="0 0 24 24" fill="currentColor">
              <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z" />
            </svg>
          </div>
          <span className="text-sm font-medium text-[#25D366]">WhatsApp</span>
          <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
        </div>
        <h2 className="text-lg font-semibold text-[#2C2C2A]">{item.title}</h2>
      </div>
      {/* Message bubble */}
      <div className="flex-1 overflow-auto p-5 pb-24" data-inbox-scroll="true">
        <div className="max-w-md">
          <div className="bg-[#E8F5E8] rounded-xl rounded-tl-sm px-4 py-3 mb-4">
            <p className="text-sm text-[#2C2C2A] whitespace-pre-wrap">{item.content}</p>
            <p className="text-[10px] text-[#5F5E5A] mt-1 text-right">{timeAgo(item.created_at)}</p>
          </div>
        </div>
      </div>
      {/* Reply box */}
      <div className="border-t border-[#E0DED8] p-4">
        <div className="flex items-end gap-2">
          <textarea
            value={waReplyText}
            onChange={e => setWaReplyText(e.target.value)}
            placeholder="Type a reply..."
            rows={2}
            className="flex-1 px-3 py-2 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] resize-none focus:outline-none focus:ring-2 focus:ring-[#25D366]/20 focus:border-[#25D366]"
          />
          <button
            onClick={() => handleWhatsAppReply(item)}
            disabled={waReplying || !waReplyText.trim()}
            className="px-4 py-2 bg-[#25D366] text-white rounded-lg text-sm font-medium hover:bg-[#1da851] transition-colors disabled:opacity-50 flex items-center gap-1.5"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
            </svg>
            {waReplying ? "Sending..." : "Reply"}
          </button>
        </div>
      </div>
    </div>
  );

  const renderStandardDetail = (item: InboxItem) => {
    const isEditing = editingId === item.id;
    return (
    <div className="flex flex-col w-full">
      <div className="border-b border-[#E0DED8] p-5">
        <div className="flex items-center gap-2 mb-2">
          <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }} />
          <span className="text-sm font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
            {AGENT_NAMES[item.agent] || item.agent}
          </span>
          <span className="text-xs text-[#9E9C95]">{typeLabel(item.type)}</span>
          <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
        </div>
        {isEditing ? (
          <input
            value={editDraft?.title ?? ""}
            onChange={(e) => setEditDraft({ ...editDraft, title: e.target.value })}
            className="w-full text-lg font-semibold text-[#2C2C2A] bg-white border border-[#534AB7]/40 rounded-md px-2 py-1 focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30"
            placeholder="Title"
          />
        ) : (
          <h2 className="text-lg font-semibold text-[#2C2C2A]">{item.title}</h2>
        )}
        <div className="flex items-center gap-2 mt-3 flex-wrap">
          {/* Edit / Save / Cancel — available for every item type. For
              images the textarea edits the prompt/description; the
              image render itself stays read-only (refining the image
              is a separate re-dispatch flow). */}
          {!isEditing && (
            <button
              onClick={() => beginEditContent(item)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-white text-[#534AB7] border border-[#534AB7] hover:bg-[#EEEDFE] transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M16.862 4.487l1.687-1.688a1.875 1.875 0 112.652 2.652L10.582 16.07a4.5 4.5 0 01-1.897 1.13L6 18l.8-2.685a4.5 4.5 0 011.13-1.897l8.932-8.931zM19.5 19.5h-15" />
              </svg>
              Edit
            </button>
          )}
          {isEditing && (
            <>
              <button
                onClick={() => handleSaveEdit(item)}
                disabled={editSaving}
                className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors disabled:opacity-60"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 12.75l6 6 9-13.5" />
                </svg>
                {editSaving ? "Saving..." : "Save"}
              </button>
              <button
                onClick={cancelEdit}
                disabled={editSaving}
                className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors disabled:opacity-60"
              >
                Cancel
              </button>
            </>
          )}
          <button
            onClick={() => handleCopy(item.content)}
            className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
            </svg>
            {copied ? "Copied!" : "Copy content"}
          </button>
          {item.type === "image" && (
            <button
              onClick={() => handleDownloadImage(item)}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-white text-[#534AB7] border border-[#534AB7] hover:bg-[#EEEDFE] transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M3 16.5v2.25A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75V16.5M16.5 12L12 16.5m0 0L7.5 12m4.5 4.5V3" />
              </svg>
              Download
            </button>
          )}
          {/* Schedule button -- available for ANY non-failed/non-cancelled item.
              The user explicitly asked for this on every sub-agent output, not
              just emails. The schedule modal/handler picks the right task_type
              based on item.type / item.email_draft. */}
          {item.status !== "failed" && item.status !== "cancelled" && item.status !== "sent" && (
            <button
              onClick={() => { setScheduleItem(item); setSchedulePlatform(""); }}
              className="flex items-center gap-1.5 px-3 py-1.5 text-sm font-medium rounded-lg bg-white text-[#534AB7] border border-[#534AB7] hover:bg-[#EEEDFE] transition-colors"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6.75 3v2.25M17.25 3v2.25M3 18.75V7.5a2.25 2.25 0 012.25-2.25h13.5A2.25 2.25 0 0121 7.5v11.25m-18 0A2.25 2.25 0 005.25 21h13.5A2.25 2.25 0 0021 18.75m-18 0v-7.5A2.25 2.25 0 015.25 9h13.5A2.25 2.25 0 0121 11.25v7.5" /></svg>
              Schedule
            </button>
          )}
          {item.status === "ready" && (
            <button onClick={() => handleStatusChange(item, "completed")} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors">
              Mark complete
            </button>
          )}
          {item.status === "completed" && (
            <button onClick={() => handleStatusChange(item, "ready")} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors">
              Reopen
            </button>
          )}
          {item.status === "cancelled" ? (
            <>
              <button onClick={() => handleRestore(item)} className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors">
                Restore
              </button>
              <button onClick={() => handleDeletePermanently(item)} className="px-3 py-1.5 text-sm font-medium rounded-lg border border-red-300 text-red-500 hover:bg-red-50 transition-colors">
                Delete forever
              </button>
            </>
          ) : (
            <button onClick={() => handleDelete(item)} className="ml-auto px-3 py-1.5 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors">
              Delete
            </button>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-auto p-5 pb-24" data-inbox-scroll="true">
        {isEditing ? (
          <div className="space-y-3">
            {/* Image items keep the rendered asset visible while the user
                edits the description / prompt text below. The image
                itself isn't user-editable here — re-generation happens
                through Media Agent re-dispatch. */}
            {item.type === "image" && (() => {
              const thumb = getInboxThumbnail(item);
              if (!thumb) return null;
              return (
                <div className="rounded-xl overflow-hidden border border-[#E0DED8] bg-[#F8F8F6]">
                  <img
                    src={thumb}
                    alt=""
                    className="w-full h-auto object-cover max-h-[360px]"
                    loading="lazy"
                  />
                </div>
              );
            })()}
            <textarea
              value={editDraft?.content ?? ""}
              onChange={(e) => setEditDraft({ ...editDraft, content: e.target.value })}
              placeholder={item.type === "image" ? "Edit the prompt or description" : "Edit content (supports markdown)"}
              className="w-full min-h-[360px] font-mono text-sm p-3 rounded-lg border border-[#E0DED8] bg-white focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7]/60 resize-y"
            />
          </div>
        ) : (
          <div className="prose prose-sm max-w-none text-[#2C2C2A]">
            {/* Attached-media preview — renders above the text when the
                row has a resolvable image URL (metadata.image_url,
                email_draft.image_urls[0], or an inline URL in the
                content). Gives the user a visual confirmation that
                the deliverable includes the image the agent was
                supposed to attach, without having to parse the text. */}
            {(() => {
              const thumb = getInboxThumbnail(item);
              if (!thumb) return null;
              return (
                <div className="mb-4 rounded-xl overflow-hidden border border-[#E0DED8] bg-[#F8F8F6]">
                  <img
                    src={thumb}
                    alt=""
                    className="w-full h-auto object-cover max-h-[360px]"
                    loading="lazy"
                  />
                </div>
              );
            })()}
            {(() => {
              // Defensive: a corrupt sub-agent reply or a placeholder
              // row that never got filled in can leave content as null
              // / undefined / "". `.includes()` on null throws, which
              // would white-screen the detail pane silently. Coerce to
              // empty string and render an explicit empty-state.
              const body = item.content || "";
              if (!body.trim()) {
                const thumb = getInboxThumbnail(item);
                if (thumb) {
                  // Image-only row (Media Designer with no caption) —
                  // the thumbnail above already covers the deliverable.
                  return null;
                }
                return (
                  <p className="text-sm text-[#9E9C95] italic">
                    This item doesn&rsquo;t have a body yet. The agent may
                    still be working on it, or the run finished without
                    posting content. Try Reopen / re-dispatching from CEO
                    chat.
                  </p>
                );
              }
              if (looksLikeHtml(body)) {
                return <div className="whitespace-pre-wrap">{stripHtml(body)}</div>;
              }
              if (body.includes("## ") || body.includes("**")) {
                return renderMarkdown(body);
              }
              return <div className="whitespace-pre-wrap">{body}</div>;
            })()}
          </div>
        )}
      </div>
    </div>
    );
  };

  return (
    <div className="max-w-screen-2xl mx-auto space-y-4">
      <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">Inbox</h1>
      <p className="text-sm text-[#5F5E5A] -mt-2">
        Content and deliverables from your marketing agents
      </p>

      {/* Tabs — wrapped in a relative div with a right-edge gradient
          fade so users see at a glance that there's more content past
          the visible edge. The fade is pointer-events:none so it
          doesn't intercept taps on the last visible tab. */}
      <div className="relative sticky top-14 lg:top-0 z-30">
        <div className="flex items-center gap-1 bg-white rounded-xl border border-[#E0DED8] p-1.5 overflow-x-auto shadow-sm/5 [-webkit-overflow-scrolling:touch] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {STATUS_TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => { setActiveTab(tab.key); setSelected(null); setCheckedIds(new Set()); setPage(1); setIsDeleteMode(false); }}
            className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-sm font-medium whitespace-nowrap transition-colors ${
              activeTab === tab.key
                ? "bg-[#EEEDFE] text-[#534AB7]"
                : "text-[#5F5E5A] hover:bg-[#F8F8F6]"
            }`}
          >
            {tab.label}
            {(() => {
              const count = tab.key === "" ? (statusCounts.all || 0) : (statusCounts[tab.key] || 0);
              if (count === 0) return null;
              return (
                <span className={`text-xs px-1.5 py-0.5 rounded-full ${
                  activeTab === tab.key ? "bg-[#534AB7] text-white" : "bg-[#F0F0EE] text-[#5F5E5A]"
                }`}>
                  {count}
                </span>
              );
            })()}
          </button>
          ))}
        </div>
        {/* Right-edge fade — purely cosmetic affordance that there's
            more to scroll. Same color as the tab card so it blends
            in. pointer-events:none so the gradient never eats taps. */}
        <div className="pointer-events-none absolute right-0 top-0 bottom-0 w-10 bg-gradient-to-l from-white to-transparent rounded-r-xl" />
      </div>

      {/* Content */}
      {loading ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[400px] flex items-center justify-center">
          <div className="animate-pulse text-sm text-[#5F5E5A]">Loading inbox...</div>
        </div>
      ) : filteredItems.length === 0 ? (
        <div className="bg-white rounded-xl border border-[#E0DED8] min-h-[400px] flex items-center justify-center">
          <div className="text-center px-6 py-16">
            <div className="w-16 h-16 rounded-full bg-[#F8F8F6] flex items-center justify-center mx-auto mb-4">
              <svg className="w-8 h-8 text-[#E0DED8]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 13.5h3.86a2.25 2.25 0 012.012 1.244l.256.512a2.25 2.25 0 002.013 1.244h3.218a2.25 2.25 0 002.013-1.244l.256-.512a2.25 2.25 0 012.013-1.244h3.859M12 3v8.25m0 0l-3-3m3 3l3-3" />
              </svg>
            </div>
            <h3 className="text-base font-semibold text-[#2C2C2A] mb-1">No deliverables yet</h3>
            <p className="text-sm text-[#5F5E5A] max-w-sm mx-auto">
              Ask the CEO agent to create content — blog posts, emails, social posts, or ad campaigns will appear here.
            </p>
            <a href="/chat" className="inline-block mt-4 text-sm font-medium text-[#534AB7] hover:underline">
              Chat with CEO to get started
            </a>
          </div>
        </div>
      ) : (
        <div className="@container/inbox">
        <div className="flex flex-col @3xl/inbox:flex-row gap-4 @3xl/inbox:min-h-[500px] @3xl/inbox:h-[calc(100dvh-220px)]">
          {/* Item list — switches between stacked (mobile) and side-by-
              side master-detail based on the *container's* width via
              `@container/inbox` (declared on the wrapper above).
              Two gotchas drove the current shape:
                1. `@container/inbox` and `@3xl/inbox:*` must NOT live
                   on the same element. Self-referential container
                   queries are legal CSS but unreliable in practice —
                   the master-detail layout silently stayed flex-col
                   on wide desktop. Wrapping the declaration on a
                   parent div fixes it (see outer `@container/inbox`).
                2. The conditional uses bare `hidden`/`flex` plus
                   `@3xl/inbox:flex` as the wide-width override. We do
                   NOT use `@max-3xl/inbox:hidden` — that variant only
                   exists in newer plugin versions, and we're pinned
                   to `@tailwindcss/container-queries@0.1.1` which
                   only emits min-width (`@`) variants. The class
                   would silently compile to nothing, leaving both
                   panes always visible on mobile and stacking them
                   vertically. With (1) fixed and the cascade order
                   correct (Tailwind emits plugin variants AFTER base
                   utilities, so `@3xl/inbox:flex` wins over `hidden`
                   when the container is wide enough), the bare
                   conditional pattern works on both viewports. */}
          <div className={`${mobileShowDetail ? "hidden" : "flex"} @3xl/inbox:flex w-full @3xl/inbox:w-[380px] shrink-0 flex-col gap-2 overflow-hidden`}>
            {/* Delete Mode toggle + bulk action toolbar.
                Default (View Mode): just a single "Delete" button on the
                right, nothing else. Clicking it flips to Delete Mode,
                which reveals the Select All row, per-row checkboxes
                (via isDeleteMode prop), and the bulk action buttons
                when something is checked. Cancel / successful bulk
                action exits the mode cleanly. */}
            <div className="flex items-center gap-2 px-1 min-h-[34px]">
              {!isDeleteMode ? (
                <button
                  onClick={() => setIsDeleteMode(true)}
                  className="ml-auto flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] bg-white hover:border-[#534AB7]/40 hover:text-[#534AB7] hover:bg-[#FAFAFF] transition-colors"
                  aria-label="Enter delete mode"
                  title="Select items to bulk-cancel"
                >
                  <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" d="M9 12h6m-3-3v6m-7.5 0a7.5 7.5 0 1015 0 7.5 7.5 0 00-15 0z M14.74 9l-.346 9m-4.788 0L9.26 9" />
                  </svg>
                  Delete
                </button>
              ) : (
                <>
                  <label className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={filteredItems.length > 0 && checkedIds.size === filteredItems.length}
                      onChange={toggleAllChecked}
                      className="w-4 h-4 rounded border-[#C5C3BC] text-[#534AB7] focus:ring-[#534AB7] cursor-pointer"
                    />
                    <span className="text-xs text-[#5F5E5A]">
                      {checkedIds.size > 0 ? `${checkedIds.size} selected` : "Select all"}
                    </span>
                  </label>
                  <div className="flex items-center gap-1.5 ml-auto">
                    {checkedIds.size > 0 && (
                      <button
                        onClick={async () => {
                          await handleBulkDelete();
                          setIsDeleteMode(false);
                        }}
                        disabled={actionLoading === "bulk-delete"}
                        className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-lg bg-red-500 text-white hover:bg-red-600 transition-colors disabled:opacity-60"
                      >
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" />
                        </svg>
                        {actionLoading === "bulk-delete" ? "Moving..." : `Move to Cancelled (${checkedIds.size})`}
                      </button>
                    )}
                    <button
                      onClick={() => {
                        setIsDeleteMode(false);
                        setCheckedIds(new Set());
                      }}
                      className="px-2.5 py-1.5 text-xs font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                </>
              )}
            </div>

            <div className="flex-1 overflow-y-auto -mr-1 pr-1 space-y-2" data-inbox-list-scroll="true">
            {filteredItems.map((item, idx) => {
              const badge = STATUS_BADGES[item.status];
              const isChecked = checkedIds.has(item.id);
              const isKeyboardFocused = idx === keyboardIndex;
              const isHighlighted = highlightedId === item.id;
              return (
                <div
                  key={item.id}
                  data-inbox-item={item.id}
                  className={`flex items-start gap-2 p-4 rounded-xl border transition-all cursor-pointer min-w-0 ${
                    isHighlighted
                      ? "border-[#534AB7] bg-[#EEEDFE] shadow-md ring-2 ring-[#534AB7]/40 animate-pulse"
                      : selected?.id === item.id
                      ? "border-[#534AB7] bg-[#FAFAFF] shadow-sm"
                      : isChecked
                      ? "border-[#534AB7]/40 bg-[#FAFAFF]/50"
                      : isKeyboardFocused
                      ? "border-[#534AB7]/60 bg-white ring-1 ring-[#534AB7]/30"
                      : "border-[#E0DED8] bg-white hover:border-[#C5C3BC]"
                  }`}
                >
                  <input
                    type="checkbox"
                    checked={isChecked}
                    onChange={(e) => { e.stopPropagation(); toggleCheck(item.id); }}
                    className={`mt-0.5 rounded border-[#C5C3BC] text-[#534AB7] focus:ring-[#534AB7] cursor-pointer shrink-0 transition-all duration-200 ${
                      isDeleteMode ? "w-4 h-4 opacity-100" : "w-0 h-0 opacity-0 overflow-hidden pointer-events-none"
                    }`}
                  />
                  <button
                    onClick={() => { setSelected(item); setMobileShowDetail(true); }}
                    className="flex-1 text-left min-w-0"
                  >
                    <div className="flex items-center gap-2 mb-1.5">
                      <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: AGENT_COLORS[item.agent] || "#999" }} />
                      <span className="text-xs font-medium" style={{ color: AGENT_COLORS[item.agent] || "#999" }}>
                        {AGENT_NAMES[item.agent] || item.agent}
                      </span>
                      <span className="text-xs text-[#9E9C95] ml-auto">{timeAgo(item.created_at)}</span>
                    </div>
                    <div className="flex items-start gap-3">
                      <div className="flex-1 min-w-0">
                        <h4 className="text-sm font-semibold text-[#2C2C2A] truncate">{item.title}</h4>
                        {/* Email rows get their dedicated preview_snippet
                            (subject's HTML body distilled by the parser).
                            Every other row falls through to a plain-text
                            excerpt of `content` so the card shows what's
                            actually inside instead of just the title. */}
                        {item.email_draft?.preview_snippet ? (
                          <p className="text-xs text-[#9E9C95] mt-1 line-clamp-2">{item.email_draft.preview_snippet}</p>
                        ) : (() => {
                          const excerpt = getInboxExcerpt(item);
                          return excerpt ? (
                            <p className="text-xs text-[#9E9C95] mt-1 line-clamp-2">{excerpt}</p>
                          ) : null;
                        })()}
                      </div>
                      {(() => {
                        const thumb = getInboxThumbnail(item);
                        if (!thumb) return null;
                        return (
                          <img
                            src={thumb}
                            alt=""
                            className="w-12 h-12 rounded-md object-cover border border-[#E0DED8] shrink-0"
                            loading="lazy"
                          />
                        );
                      })()}
                    </div>
                    <div className="flex items-center gap-2 mt-2">
                      <span className="text-[11px] px-2 py-0.5 rounded-full bg-[#F8F8F6] text-[#5F5E5A] border border-[#E0DED8]">
                        {typeLabel(item.type)}
                      </span>
                      <span className={`w-1.5 h-1.5 rounded-full ${PRIORITY_DOT[item.priority] || "bg-gray-400"}`} />
                      <span className="text-[11px] text-[#9E9C95] capitalize">{item.priority}</span>
                      {badge && (
                        <span className={`ml-auto text-[11px] px-2 py-0.5 rounded-full border ${badge.bg} ${badge.text} ${badge.border}`}>
                          {badge.label}
                        </span>
                      )}
                    </div>
                  </button>
                  {item.status === "processing" && (
                    <button
                      onClick={(e) => handleCancelProcessing(item, e)}
                      className="shrink-0 self-center px-2 py-1 text-[11px] font-medium rounded-lg border border-red-200 text-red-400 hover:bg-red-50 hover:text-red-600 transition-colors"
                      title="Cancel this in-progress task"
                    >
                      Cancel
                    </button>
                  )}
                </div>
              );
            })}

            {/* Pagination — compact horizontal bar. Previous version
                wrapped "Page 1 of 6" vertically and overflowed "Last"
                off the right edge in the narrow inbox-list column.
                Now: single horizontal row, page indicator inline with
                "1/6 · 117", First/Last icon-only (saves space, still
                hit-target sized), Prev/Next keep their label AND
                icon, everything wraps sanely on sub-320px widths. */}
            {totalPages > 1 && (
              <div className="mt-4 flex flex-wrap items-center justify-between gap-2 bg-gradient-to-r from-[#EEEDFE] to-[#F8F8F6] border border-[#534AB7]/20 rounded-xl px-3 py-2 shadow-sm">
                <div className="flex items-center gap-2 min-w-0">
                  <div className="flex items-center justify-center px-2 h-8 min-w-[32px] rounded-md bg-[#534AB7] text-white text-sm font-bold shadow-sm">
                    {page}
                  </div>
                  <span className="text-xs font-medium text-[#2C2C2A] whitespace-nowrap">
                    of {totalPages} · <span className="text-[#5F5E5A]">{totalItems} items</span>
                  </span>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => { setPage(1); setCheckedIds(new Set()); setSelected(null); setKeyboardIndex(0); }}
                    disabled={page <= 1}
                    className="p-2 rounded-md border border-[#534AB7]/30 bg-white text-[#534AB7] hover:bg-[#534AB7] hover:text-white disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-white disabled:hover:text-[#534AB7] transition-all"
                    aria-label="First page"
                    title="First page"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M18.75 19.5l-7.5-7.5 7.5-7.5m-6 15L5.25 12l7.5-7.5" />
                    </svg>
                  </button>
                  <button
                    onClick={() => { setPage((p) => Math.max(1, p - 1)); setCheckedIds(new Set()); setSelected(null); setKeyboardIndex(0); }}
                    disabled={page <= 1}
                    className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-md border border-[#534AB7]/30 bg-white text-[#534AB7] hover:bg-[#534AB7] hover:text-white disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-white disabled:hover:text-[#534AB7] transition-all"
                    aria-label="Previous page"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 19.5L8.25 12l7.5-7.5" />
                    </svg>
                    Prev
                  </button>
                  <button
                    onClick={() => { setPage((p) => Math.min(totalPages, p + 1)); setCheckedIds(new Set()); setSelected(null); setKeyboardIndex(0); }}
                    disabled={page >= totalPages}
                    className="flex items-center gap-1 px-2.5 py-1.5 text-xs font-medium rounded-md bg-[#534AB7] text-white hover:bg-[#433AA0] disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-[#534AB7] transition-all shadow-sm"
                    aria-label="Next page"
                  >
                    Next
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 4.5l7.5 7.5-7.5 7.5" />
                    </svg>
                  </button>
                  <button
                    onClick={() => { setPage(totalPages); setCheckedIds(new Set()); setSelected(null); setKeyboardIndex(0); }}
                    disabled={page >= totalPages}
                    className="p-2 rounded-md border border-[#534AB7]/30 bg-white text-[#534AB7] hover:bg-[#534AB7] hover:text-white disabled:opacity-30 disabled:cursor-not-allowed disabled:hover:bg-white disabled:hover:text-[#534AB7] transition-all"
                    aria-label="Last page"
                    title="Last page"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M5.25 4.5l7.5 7.5-7.5 7.5m6-15l7.5 7.5-7.5 7.5" />
                    </svg>
                  </button>
                </div>
              </div>
            )}
            </div>
          </div>

          {/* Detail pane. On mobile, hidden until the user taps an item; on
              desktop (md+) always visible. The "Back to inbox" header bar
              only renders on mobile (md:hidden) and lets the user pop back
              to the list. Without this, mobile users could tap items but
              never see the content (the previous design was hidden md:flex
              which made the inbox effectively read-only-of-titles on phones).
              Height-constrained + overflow-hidden so the inner renderer's
              scroll container owns the scrolling (sticky master-detail). */}
          <div
            ref={detailPaneRef}
            className={`${mobileShowDetail ? "flex" : "hidden"} @3xl/inbox:flex flex-1 bg-white rounded-xl border border-[#E0DED8] overflow-hidden flex-col`}
          >
            {selected ? (
              <>
                {/* Mobile-only back button -- the desktop layout has both
                    panes side-by-side so this header is unnecessary there. */}
                <div className="md:hidden flex items-center gap-2 px-4 py-3 border-b border-[#E0DED8] bg-white">
                  <button
                    onClick={() => { setMobileShowDetail(false); }}
                    className="flex items-center gap-1.5 text-sm font-medium text-[#534AB7] hover:text-[#433AA0]"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M15 19l-7-7 7-7" />
                    </svg>
                    Back to inbox
                  </button>
                </div>
                <div className="flex-1 flex overflow-hidden">
                  {isEmailDraft(selected)
                    ? (isPendingApproval(selected) ? renderEmailEditor(selected) : renderEmailReadOnly(selected))
                    : isSocialPost(selected) ? renderSocialDetail(selected)
                    : selected.type === "whatsapp_message" ? renderWhatsAppDetail(selected)
                    : renderStandardDetail(selected)}
                </div>
              </>
            ) : (
              <div className="flex items-center justify-center w-full text-sm text-[#9E9C95]">
                Select an item to view its content
              </div>
            )}
          </div>
        </div>
        </div>
      )}
      {/* Schedule Picker Modal */}
      {scheduleItem && (
        <div className="fixed inset-0 z-[70] flex items-center justify-center bg-black/30" onClick={() => setScheduleItem(null)}>
          <div className="bg-white rounded-xl border border-[#E0DED8] shadow-2xl w-[calc(100vw-2rem)] max-w-[400px] mx-4" onClick={(e) => e.stopPropagation()}>
            <div className="flex items-center justify-between px-5 py-3 border-b border-[#E0DED8]">
              <h3 className="text-sm font-semibold text-[#2C2C2A]">Schedule Task</h3>
              <button onClick={() => setScheduleItem(null)} className="text-[#B0AFA8] hover:text-[#2C2C2A]">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div className="px-5 py-4 space-y-4">
              <div className="text-sm text-[#2C2C2A] font-medium truncate">{scheduleItem.title}</div>
              {isSocialPost(scheduleItem) && (
                <div>
                  <label className="block text-xs font-medium text-[#5F5E5A] mb-1">Platform</label>
                  <select value={schedulePlatform} onChange={(e) => setSchedulePlatform(e.target.value)} className="w-full px-3 py-2 border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A]">
                    <option value="">Auto-detect</option>
                    <option value="twitter">X / Twitter</option>
                    <option value="linkedin">LinkedIn</option>
                  </select>
                </div>
              )}
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-xs font-medium text-[#5F5E5A] mb-1">Date</label>
                  <input type="date" value={scheduleDate} onChange={(e) => setScheduleDate(e.target.value)} min={new Date().toISOString().split("T")[0]} className="w-full px-3 py-2 border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A]" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-[#5F5E5A] mb-1">Time</label>
                  <input type="time" value={scheduleTime} onChange={(e) => setScheduleTime(e.target.value)} className="w-full px-3 py-2 border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A]" />
                </div>
              </div>
              <p className="text-[10px] text-[#9E9C95]">Timezone: {Intl.DateTimeFormat().resolvedOptions().timeZone}</p>
              <div className="flex items-center gap-2 pt-2 border-t border-[#E0DED8]">
                <button onClick={handleSchedule} disabled={scheduling || !scheduleDate} className="flex-1 px-4 py-2 bg-[#534AB7] text-white text-sm font-medium rounded-lg hover:bg-[#433AA0] transition-colors disabled:opacity-50">
                  {scheduling ? "Scheduling..." : "Schedule"}
                </button>
                <button onClick={() => setScheduleItem(null)} className="px-4 py-2 text-sm text-[#5F5E5A] hover:text-[#2C2C2A]">Cancel</button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
