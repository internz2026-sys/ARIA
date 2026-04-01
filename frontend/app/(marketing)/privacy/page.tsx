import { Shield, Database, Eye, Lock, Globe, Clock, UserCheck, Cookie, Baby, Bell, Mail } from "lucide-react";

const sections = [
  {
    icon: Eye,
    title: "1. Introduction",
    content: `ARIA ("we", "our", "us") is an AI-powered marketing platform that helps developer founders grow their businesses. This Privacy Policy explains how we collect, use, disclose, and safeguard your information when you use our platform and related services.\n\nBy using ARIA, you agree to the collection and use of information in accordance with this policy. If you do not agree, please do not use the platform.`,
  },
  {
    icon: Database,
    title: "2. Information We Collect",
    subsections: [
      {
        subtitle: "Account Information",
        items: ["Name and email address", "Business name and description", "Product information and target audience details", "Brand voice preferences"],
      },
      {
        subtitle: "Connected Services Data",
        items: [
          "Gmail: Ability to send emails and read inbox for reply drafting. We access email content only to draft replies and send approved messages on your behalf.",
          "X/Twitter: Ability to post tweets on your behalf. We access your profile information and posting permissions.",
          "LinkedIn: Ability to publish posts to your profile or company pages. We access your profile and organization admin status.",
          "WhatsApp: Ability to send and receive business messages. We store your WhatsApp Business API credentials.",
        ],
      },
      {
        subtitle: "Usage Data",
        items: [
          "Agent activity logs (which agents ran, when, and what they produced)",
          "Chat conversation history with the CEO agent",
          "API usage metrics (requests, tokens consumed)",
          "CRM data you create (contacts, companies, deals)",
        ],
      },
    ],
  },
  {
    icon: Shield,
    title: "3. How We Use Your Information",
    items: [
      "Provide, operate, and maintain the ARIA platform",
      "Generate personalized marketing content, strategies, and recommendations",
      "Send emails, publish social posts, and execute marketing actions you approve",
      "Improve our AI agents and the quality of generated content",
      "Communicate with you about your account and platform updates",
      "Ensure platform security and prevent abuse",
    ],
  },
  {
    icon: Globe,
    title: "4. How We Share Your Information",
    content: "We do not sell your personal information. We share data only with:",
    items: [
      "Service providers: Supabase (database), Anthropic (AI processing), Vercel (hosting), Railway (backend hosting)",
      "Connected platforms: Only when you explicitly authorize actions (e.g., posting a tweet, sending an email)",
      "Legal requirements: When required by law or to protect our rights",
    ],
  },
  {
    icon: Lock,
    title: "5. Data Security",
    content: "We implement industry-standard security measures including:",
    items: [
      "JWT-based authentication on all API endpoints",
      "Tenant isolation — users can only access their own data",
      "CORS restrictions to authorized domains",
      "Rate limiting to prevent abuse",
      "OAuth 2.0 for all third-party integrations (no passwords stored)",
      "HTTPS encryption for all data in transit",
    ],
    footer: "While we strive to protect your data, no method of electronic transmission or storage is 100% secure.",
  },
  {
    icon: Globe,
    title: "6. Third-Party Services",
    content: "ARIA integrates with third-party services that have their own privacy policies. We encourage you to review:",
    links: [
      { label: "Google Privacy Policy", href: "https://policies.google.com/privacy" },
      { label: "X/Twitter Privacy Policy", href: "https://x.com/en/privacy" },
      { label: "LinkedIn Privacy Policy", href: "https://www.linkedin.com/legal/privacy-policy" },
      { label: "WhatsApp Privacy Policy", href: "https://www.whatsapp.com/legal/privacy-policy" },
    ],
  },
  {
    icon: Clock,
    title: "7. Data Retention",
    content: "We retain your data for as long as your account is active or as needed to provide services. You may request deletion of your account and associated data at any time by contacting us. Upon deletion request, we will remove your data within 30 days, except where retention is required by law.",
  },
  {
    icon: UserCheck,
    title: "8. Your Rights",
    items: [
      "Access your personal data",
      "Correct inaccurate data",
      "Delete your account and data",
      "Disconnect any third-party integration at any time",
      "Export your data",
      "Withdraw consent for data processing",
    ],
  },
  {
    icon: Cookie,
    title: "9. Cookies",
    content: "We use essential cookies and local storage for authentication and session management. We do not use tracking cookies or third-party advertising cookies.",
  },
  {
    icon: Baby,
    title: "10. Children's Privacy",
    content: "ARIA is not intended for users under the age of 18. We do not knowingly collect personal information from children.",
  },
  {
    icon: Bell,
    title: "11. Changes to This Policy",
    content: 'We may update this Privacy Policy from time to time. We will notify you of any material changes by posting the new policy on this page and updating the "Last updated" date.',
  },
  {
    icon: Mail,
    title: "12. Contact Us",
    content: "If you have questions about this Privacy Policy, please contact us at:",
    contact: "support@aria.ai",
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
          <p className="text-[#9E9C95] text-sm">Last updated: April 1, 2026</p>
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
