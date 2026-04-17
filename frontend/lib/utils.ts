import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function formatCurrency(amount: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 0 }).format(amount);
}

export function formatDate(date: Date | string): string {
  return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", year: "numeric" }).format(new Date(date));
}

export function formatNumber(num: number): string {
  if (num >= 1000000) return `${(num / 1000000).toFixed(1)}M`;
  if (num >= 1000) return `${(num / 1000).toFixed(1)}K`;
  return num.toString();
}

export function formatDateAgo(dateStr: string): string {
  const diffMs = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diffMs / 60000);
  if (mins < 1) return "Just now";
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(dateStr).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function getGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return "Good morning";
  if (h < 17) return "Good afternoon";
  return "Good evening";
}

export function getInitials(name: string): string {
  return name.split(" ").map((w) => w[0]).join("").toUpperCase().slice(0, 2);
}

/** Strip markdown syntax down to a plain-text preview.
 *
 * Used by the notification bell + any other small-surface preview where
 * we want a human-readable summary instead of raw agent output. Not a
 * full markdown parser — just cheap regex passes that remove the
 * characters users don't want to see (`##`, `**`, `_`, `>`, bullets,
 * fenced code, inline links) while preserving the underlying text.
 *
 * Examples:
 *   "## New Blog Post"              -> "New Blog Post"
 *   "**Urgent:** Task ready"        -> "Urgent: Task ready"
 *   "- Item one\n- Item two"        -> "Item one, Item two"
 *   "[Click here](https://...)"     -> "Click here"
 *   "> quoted text"                 -> "quoted text"
 */
export function stripMarkdown(text: string): string {
  if (!text) return "";
  let out = text;

  // Fenced code blocks — drop entirely, a preview doesn't need them.
  out = out.replace(/```[\s\S]*?```/g, " ");
  // Inline code — keep the contents, drop the backticks.
  out = out.replace(/`([^`]+)`/g, "$1");
  // Inline links `[text](url)` — keep just the text.
  out = out.replace(/\[([^\]]+)\]\([^)]*\)/g, "$1");
  // Images `![alt](url)` — drop entirely (no alt shown in a one-liner).
  out = out.replace(/!\[[^\]]*\]\([^)]*\)/g, "");
  // Bold **x** / __x__ -> x
  out = out.replace(/\*\*([^*]+)\*\*/g, "$1");
  out = out.replace(/__([^_]+)__/g, "$1");
  // Italic *x* / _x_ -> x
  out = out.replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, "$1$2");
  out = out.replace(/(^|[^_])_([^_\n]+)_(?!_)/g, "$1$2");
  // Strikethrough ~~x~~ -> x
  out = out.replace(/~~([^~]+)~~/g, "$1");
  // Leading markers on a line: headings, blockquotes, bullets, ordered lists.
  out = out.replace(/^\s{0,3}#{1,6}\s+/gm, "");
  out = out.replace(/^\s{0,3}>\s?/gm, "");
  out = out.replace(/^\s{0,3}[-*+]\s+/gm, "");
  out = out.replace(/^\s{0,3}\d+\.\s+/gm, "");
  // Horizontal rules
  out = out.replace(/^\s*---+\s*$/gm, " ");
  // Stray formatting chars that survived the passes above
  out = out.replace(/[*_>#]+/g, " ");
  // Collapse whitespace (newlines -> single space) so the preview is one line.
  out = out.replace(/\s+/g, " ").trim();
  return out;
}

/** Clean JSON/code artifacts AND markdown syntax from notification body
 * text. The bell shows this as a preview subtitle — raw agent output
 * (fenced ```json blocks, ## headings, **bold** markers) shouldn't leak
 * into non-technical user views. */
export function cleanNotificationBody(body: string): string {
  if (!body) return "";
  let text = body.replace(/```\w*\n?/g, "").trim();
  // If it looks like JSON, extract readable text
  const jsonStart = text.search(/[{\[]/);
  if (jsonStart >= 0) {
    try {
      const jsonStr = text.slice(jsonStart);
      const parsed = JSON.parse(jsonStr.slice(0, jsonStr.lastIndexOf(jsonStr[0] === "[" ? "]" : "}") + 1));
      const data = Array.isArray(parsed) ? parsed[0] || {} : parsed;
      for (const key of ["text", "title", "description", "commentary", "body", "subject"]) {
        if (data[key]) return stripMarkdown(String(data[key])).slice(0, 200);
      }
      const posts = data.posts || [];
      if (posts[0]?.text) return stripMarkdown(posts[0].text).slice(0, 200);
    } catch {}
    text = text.replace(/[{}\[\]"\\]/g, "").replace(/\s+/g, " ").trim();
  }
  // Final pass: strip markdown so `##` / `**` / `_` / bullets don't leak.
  return stripMarkdown(text).slice(0, 200);
}
