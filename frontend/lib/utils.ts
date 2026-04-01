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

/** Clean JSON/code artifacts from notification body text. */
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
        if (data[key]) return String(data[key]).slice(0, 200);
      }
      const posts = data.posts || [];
      if (posts[0]?.text) return posts[0].text.slice(0, 200);
    } catch {}
    text = text.replace(/[{}\[\]"\\]/g, "").replace(/\s+/g, " ").trim();
  }
  return text.slice(0, 200);
}
