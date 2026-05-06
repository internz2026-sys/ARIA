import { Shield, Database, Eye, Lock, Globe, Clock, UserCheck, Cookie, Baby, Bell, Mail } from "lucide-react";

const sections = [
  {
    icon: Eye,
    title: "1. Introduction",
    content: `ARIA ("we", "our", "us") is an AI-powered marketing platform that helps developer founders grow their businesses. This Privacy Policy explains how we collect, use, disclose, and safeguard your information when you use our platform at aria.hoversight.agency and related services.\n\nBy using ARIA, you agree to the collection and use of information in accordance with this policy. If you do not agree, please do not use the platform.`,
  },
  {
    icon: Database,
    title: "2. Information We Collect",
    subsections: [
      {
        subtitle: "Account Information",
        items: ["Name and email address", "Business name and description", "Product information and target audience details", "Brand voice preferences", "Authentication credentials (managed by Supabase Auth)"],
      },
      {
        subtitle: "Google Account Data (when connected)",
        items: [
          "Basic profile: name, email address, profile picture (via Sign in with Google)",
          "Gmail (gmail.send scope only): permission to send email on your behalf when you explicitly approve a draft. We do NOT read your inbox, do NOT access your message contents, and do NOT have any other Gmail access.",
        ],
      },
      {
        subtitle: "Other Connected Services (optional, when authorized)",
        items: [
          "X/Twitter: ability to post tweets on your behalf (only after explicit approval). We access your profile information and posting permissions.",
          "LinkedIn: ability to publish posts to your profile or company pages (only after explicit approval). We access your profile and organization admin status.",
          "WhatsApp Business API: ability to send and receive business messages. We store the credentials you provide.",
        ],
      },
      {
        subtitle: "Usage Data",
        items: [
          "Agent activity logs (which agents ran, when, and what they produced)",
          "Chat conversation history with the CEO agent",
          "API usage metrics (requests, tokens consumed)",
          "CRM data you create (contacts, companies, deals)",
          "Marketing content you create or approve",
        ],
      },
    ],
  },
  {
    icon: Shield,
    title: "3. How We Use Your Information",
    items: [
      "Provide, operate, and maintain the ARIA platform",
      "Generate personalized marketing content, strategies, and recommendations using AI",
      "Send emails, publish social posts, and execute marketing actions you have explicitly approved",
      "Communicate with you about your account and platform updates",
      "Ensure platform security and prevent abuse",
      "Improve the quality of our AI-generated outputs based on aggregated, anonymized usage patterns",
    ],
  },
  {
    icon: Shield,
    title: "4. Google User Data — Limited Use Disclosure",
    content: "ARIA's use of information received from Google APIs adheres to the Google API Services User Data Policy, including the Limited Use requirements:",
    items: [
      "We use Google user data only to provide or improve user-facing features prominent in ARIA (Sign in with Google for authentication, gmail.send for sending approved marketing emails on your behalf).",
      "We do not transfer Google user data to third parties except as necessary to provide or improve those user-facing features, to comply with applicable law, or as part of a merger / acquisition / sale of assets with continued protections.",
      "We do not use Google user data to serve advertising, including retargeting, personalized, or interest-based advertising.",
      "We do not allow humans to read your Google user data unless we have your explicit affirmative consent for specific messages, it is necessary for security purposes (such as investigating abuse), or it is required by law.",
      "We do not use Google user data to train, develop, or improve generalized AI / ML models.",
    ],
  },
  {
    icon: Globe,
    title: "5. How We Share Your Information",
    content: "We do not sell your personal information. We share data only with the following service providers, each acting as a processor under our instructions:",
    items: [
      "Supabase (database, authentication) — hosts your account data, CRM rows, and content library",
      "Anthropic (Claude AI) — processes your prompts to generate marketing content; Anthropic does not train on your data per their Commercial Terms",
      "Hostinger (cloud infrastructure) — hosts the ARIA platform on a virtual private server",
      "Hostinger (mail hosting + SMTP relay) — delivers approved emails you send via ARIA from the platform's branded domain",
      "Postmark (inbound email parsing) — receives reply emails routed back to your ARIA inbox",
      "Pollinations.ai / Google Gemini (image generation) — generates marketing images based on your prompts",
      "Qdrant (vector search) — stores embeddings of your content for semantic recall",
      "Connected platforms (Gmail / Google, X/Twitter, LinkedIn, WhatsApp) — only when you explicitly authorize an action",
      "Legal requirements — when required by law or to protect our rights, your safety, or the rights of others",
    ],
  },
  {
    icon: Lock,
    title: "6. Data Security",
    content: "We implement industry-standard security measures including:",
    items: [
      "JWT-based authentication on all API endpoints, with per-tenant authorization checks on every request",
      "Tenant isolation — users can only access data belonging to their own tenant",
      "CORS restrictions to authorized origins only",
      "Per-tenant + per-IP rate limiting (Redis-backed) to prevent abuse",
      "OAuth 2.0 for all third-party integrations — we never see or store your provider passwords",
      "HMAC signature verification on all inbound webhooks (Stripe, Postmark, Resend, etc.)",
      "HTTPS / TLS encryption for all data in transit",
      "Logging redaction filters to prevent secrets leaking into server logs",
      "Container-level privilege separation — backend services run as non-root",
    ],
    footer: "While we strive to protect your data, no method of electronic transmission or storage is 100% secure.",
  },
  {
    icon: Globe,
    title: "7. Third-Party Privacy Policies",
    content: "ARIA integrates with third-party services that have their own privacy policies. We encourage you to review them:",
    links: [
      { label: "Google Privacy Policy", href: "https://policies.google.com/privacy" },
      { label: "Anthropic Privacy Policy", href: "https://www.anthropic.com/legal/privacy" },
      { label: "Supabase Privacy Policy", href: "https://supabase.com/privacy" },
      { label: "Hostinger Privacy Policy", href: "https://www.hostinger.com/privacy-policy" },
      { label: "Postmark Privacy Policy", href: "https://postmarkapp.com/privacy-policy" },
      { label: "X/Twitter Privacy Policy", href: "https://x.com/en/privacy" },
      { label: "LinkedIn Privacy Policy", href: "https://www.linkedin.com/legal/privacy-policy" },
      { label: "WhatsApp Privacy Policy", href: "https://www.whatsapp.com/legal/privacy-policy" },
    ],
  },
  {
    icon: Clock,
    title: "8. Data Retention",
    content: "We retain your data for as long as your account is active or as needed to provide services. You may request deletion of your account and associated data at any time by contacting us at the address below. Upon a verified deletion request, we will remove your personal data and Google user data within 30 days, except where retention is required by applicable law (e.g., financial records, abuse-investigation logs). Aggregated, fully anonymized data may be retained for analytics.",
  },
  {
    icon: UserCheck,
    title: "9. Your Rights",
    content: "Depending on your jurisdiction (including the EU/EEA under GDPR and California under CCPA), you may have the following rights:",
    items: [
      "Access your personal data we hold",
      "Correct inaccurate or incomplete data",
      "Delete your account and associated data",
      "Disconnect any third-party integration at any time from Settings",
      "Export your data in a machine-readable format",
      "Withdraw consent for data processing where consent is the legal basis",
      "Lodge a complaint with your local data protection authority",
      "Revoke ARIA's access to your Google account at any time via your Google Account settings: https://myaccount.google.com/permissions",
    ],
  },
  {
    icon: Cookie,
    title: "10. Cookies and Local Storage",
    content: "We use essential cookies and browser local storage for authentication, session management, and remembering your preferences within the dashboard. We do not use tracking cookies, behavioral profiling cookies, or third-party advertising cookies.",
  },
  {
    icon: Baby,
    title: "11. Children's Privacy",
    content: "ARIA is not intended for users under the age of 18 and is targeted at business operators / founders. We do not knowingly collect personal information from children. If we learn that we have collected personal data from a person under 18, we will delete it.",
  },
  {
    icon: Bell,
    title: "12. Changes to This Policy",
    content: 'We may update this Privacy Policy from time to time to reflect changes in our practices or for legal / regulatory reasons. We will notify you of any material changes by posting the new policy on this page and updating the "Last updated" date below. For significant changes, we will additionally notify registered users by email.',
  },
  {
    icon: Mail,
    title: "13. Contact Us",
    content: "If you have questions about this Privacy Policy, want to exercise any of your rights, or wish to request data deletion, please contact us at:",
    contact: "accounts@zillamedia.co",
  },
];

export default function PrivacyPolicyPage() {
  return (
    <div className="min-h-screen bg-gradient-to-b from-[#F8F8F6] to-white">
      {/* Hero */}
      <div className="bg-[#2C2C2A] text-white py-20">
        <div className="max-w-3xl mx-auto px-6 text-center">
          <div className="w-14 h-14 rounded-2xl bg-[#534AB7] flex items-center justify-center mx-auto mb-6">
            <Shield className="w-7 h-7 text-white" />
          </div>
          <h1 className="text-4xl font-bold mb-3">Privacy Policy</h1>
          <p className="text-[#9E9C95] text-sm">Last updated: May 6, 2026</p>
        </div>
      </div>

      {/* Content */}
      <div className="max-w-3xl mx-auto px-6 py-12 space-y-6">
        {sections.map((section) => {
          const Icon = section.icon;
          return (
            <div key={section.title} className="bg-white rounded-xl border border-[#E0DED8] p-6 shadow-sm">
              <div className="flex items-start gap-3 mb-4">
                <div className="w-9 h-9 rounded-lg bg-[#EEEDFE] flex items-center justify-center flex-shrink-0 mt-0.5">
                  <Icon className="w-4.5 h-4.5 text-[#534AB7]" />
                </div>
                <h2 className="text-lg font-semibold text-[#2C2C2A]">{section.title}</h2>
              </div>

              {section.content && (
                <p className="text-sm text-[#5F5E5A] leading-relaxed whitespace-pre-line ml-12">{section.content}</p>
              )}

              {section.subsections?.map((sub) => (
                <div key={sub.subtitle} className="ml-12 mt-4">
                  <h3 className="text-sm font-semibold text-[#2C2C2A] mb-2">{sub.subtitle}</h3>
                  <ul className="space-y-1.5">
                    {sub.items.map((item) => (
                      <li key={item} className="text-sm text-[#5F5E5A] flex items-start gap-2">
                        <span className="w-1.5 h-1.5 rounded-full bg-[#534AB7] mt-1.5 flex-shrink-0" />
                        {item}
                      </li>
                    ))}
                  </ul>
                </div>
              ))}

              {section.items && !section.subsections && (
                <ul className="ml-12 mt-3 space-y-1.5">
                  {section.items.map((item) => (
                    <li key={item} className="text-sm text-[#5F5E5A] flex items-start gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#534AB7] mt-1.5 flex-shrink-0" />
                      {item}
                    </li>
                  ))}
                </ul>
              )}

              {section.links && (
                <ul className="ml-12 mt-3 space-y-1.5">
                  {section.links.map((link) => (
                    <li key={link.href} className="text-sm flex items-center gap-2">
                      <span className="w-1.5 h-1.5 rounded-full bg-[#534AB7] flex-shrink-0" />
                      <a href={link.href} target="_blank" rel="noopener noreferrer" className="text-[#534AB7] hover:underline">{link.label}</a>
                    </li>
                  ))}
                </ul>
              )}

              {section.footer && (
                <p className="text-xs text-[#9E9C95] mt-3 ml-12 italic">{section.footer}</p>
              )}

              {section.contact && (
                <p className="ml-12 mt-2 text-sm font-medium text-[#534AB7]">{section.contact}</p>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
