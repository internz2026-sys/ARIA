// ---------------------------------------------------------------------------
// ARIA Agent Definitions — single source of truth for all agent metadata
// Import from here instead of redefining agents in each page.
// ---------------------------------------------------------------------------

export interface AgentDef {
  slug: string;
  name: string;
  role: string;
  description: string;
  color: string;
  model: "opus-4-6" | "sonnet-4-6" | "haiku-4-5";
  schedule: string;
}

export const AGENT_DEFS: AgentDef[] = [
  {
    slug: "ceo",
    name: "ARIA CEO",
    role: "Chief Marketing Strategist",
    description:
      "Builds GTM playbook, coordinates the marketing team, reviews performance, and adjusts strategy.",
    color: "#534AB7",
    model: "opus-4-6",
    schedule: "Always active",
  },
  {
    slug: "content_writer",
    name: "Content Writer",
    role: "Content Creation Agent",
    description:
      "Blog posts, landing page copy, product descriptions, case studies, thought leadership.",
    color: "#1D9E75",
    model: "sonnet-4-6",
    schedule: "Mon/Wed/Fri, 9:00 AM",
  },
  {
    slug: "email_marketer",
    name: "Email Marketer",
    role: "Email Campaign Agent",
    description:
      "Welcome sequences, newsletter drafts, drip campaigns, launch announcements.",
    color: "#BA7517",
    model: "sonnet-4-6",
    schedule: "Tue/Thu, 10:00 AM",
  },
  {
    slug: "social_manager",
    name: "Social Manager",
    role: "Social Media Agent",
    description:
      "Platform-specific posts, content calendar, engagement suggestions, hashtag strategy.",
    color: "#D85A30",
    model: "sonnet-4-6",
    schedule: "Daily, 8:00 AM",
  },
  {
    slug: "ad_strategist",
    name: "Ad Strategist",
    role: "Paid Ads Advisor",
    description:
      "Facebook/Meta ad copy, audience targeting, budget allocation, A/B test variants.",
    color: "#7C3AED",
    model: "sonnet-4-6",
    schedule: "Mon/Fri, 11:00 AM",
  },
  {
    slug: "media",
    name: "Media Designer",
    role: "Visual Content Creator",
    description:
      "Marketing image generation via Google Gemini, social media visuals, ad creatives, blog headers.",
    color: "#E4407B",
    model: "haiku-4-5",
    schedule: "Daily, 11:00 AM",
  },
];

// ---- Derived lookups (computed once, reused everywhere) -------------------

export const AGENT_MAP: Record<string, AgentDef> = Object.fromEntries(
  AGENT_DEFS.map((a) => [a.slug, a]),
);

export const AGENT_NAMES: Record<string, string> = Object.fromEntries(
  AGENT_DEFS.map((a) => [a.slug, a.name]),
);

export const AGENT_COLORS: Record<string, string> = Object.fromEntries(
  AGENT_DEFS.map((a) => [a.slug, a.color]),
);

export const AGENT_LABELS: Record<string, { name: string; color: string }> =
  Object.fromEntries(AGENT_DEFS.map((a) => [a.slug, { name: a.name, color: a.color }]));
