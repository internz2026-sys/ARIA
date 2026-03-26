"use client";
import { useState } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const departments = [
  {
    name: "Sales", agents: [
      { name: "Lead Gen Agent", desc: "Finds and qualifies prospects matching your ICP daily. Enriches with buying signals, scores leads 1-10.", trigger: "Daily 8am", model: "Sonnet" },
      { name: "Outreach Agent", desc: "Writes personalised cold emails. Generates 4-email sequences. A/B tests subject lines.", trigger: "New lead", model: "Sonnet" },
      { name: "Closer Agent", desc: "Handles all inbound replies. Classifies intent, overcomes objections, books demos.", trigger: "Inbound reply", model: "Opus" },
      { name: "Follow-up Agent", desc: "Re-engages cold leads with new angles. Win-back campaigns and referral requests.", trigger: "Scheduled", model: "Sonnet" },
    ],
  },
  {
    name: "Finance", agents: [
      { name: "Accounting Summary", desc: "Daily revenue summary and weekly P&L snapshot. Flags significant drops.", trigger: "Daily 7am", model: "Haiku" },
      { name: "Invoice Agent", desc: "Creates invoices on milestones. Payment reminders at 7, 14, 30 days overdue.", trigger: "Milestone", model: "Haiku" },
      { name: "Expense Alert", desc: "Monitors bank feeds. Flags unusual expenses. Auto-categorizes transactions.", trigger: "Daily sync", model: "Haiku" },
    ],
  },
  {
    name: "Customer Service", agents: [
      { name: "Support Agent", desc: "Answers FAQs 24/7. Handles complaints empathetically. Escalates complex issues.", trigger: "Inbound msg", model: "Sonnet" },
      { name: "Review Agent", desc: "Requests reviews post-purchase. Auto-responds to Google/Yelp reviews within 1 hour.", trigger: "Post-purchase", model: "Haiku" },
      { name: "Feedback Agent", desc: "Sends NPS surveys. Collects testimonials from promoters. Follows up with detractors.", trigger: "7/30/90 days", model: "Haiku" },
    ],
  },
  {
    name: "Marketing", agents: [
      { name: "Social Media Agent", desc: "Responds to DMs and comments. Drafts weekly social posts for approval.", trigger: "New DM", model: "Sonnet" },
      { name: "Content Agent", desc: "Weekly email newsletters. Promotional copy. Adapts to your brand voice.", trigger: "Weekly", model: "Sonnet" },
      { name: "Ad Monitor Agent", desc: "Tracks spend across Facebook and Google Ads. Alerts on CPC spikes or ROAS drops.", trigger: "Daily", model: "Haiku" },
    ],
  },
  {
    name: "Operations", agents: [
      { name: "CRM Agent", desc: "Logs every interaction automatically. Updates deal stages. Flags stale deals.", trigger: "Continuous", model: "Haiku" },
      { name: "Scheduling Agent", desc: "Books appointments via Calendly. Sends confirmations and reminders.", trigger: "Booking req", model: "Haiku" },
      { name: "Customer Onboarding", desc: "Walks new customers through setup. Day 0/3/7 messages. Detects churn signals.", trigger: "New customer", model: "Sonnet" },
    ],
  },
  {
    name: "Internal Ops", agents: [
      { name: "HR & Payroll Agent", desc: "Tracks attendance. Weekly payroll summaries. Leave request management.", trigger: "Daily", model: "Haiku" },
      { name: "Analytics Agent", desc: "Weekly business health report. Revenue, leads, satisfaction. One actionable recommendation.", trigger: "Monday 9am", model: "Haiku" },
    ],
  },
];

const modelColor = { Opus: "danger", Sonnet: "default", Haiku: "secondary" } as const;

export default function FeaturesPage() {
  const [active, setActive] = useState("Sales");

  return (
    <div className="py-16">
      <div className="mx-auto max-w-7xl px-4">
        <div className="text-center">
          <h1 className="text-4xl font-bold text-text-primary">18 AI agents. 6 departments. One platform.</h1>
          <p className="mt-3 text-text-secondary">Every agent is pre-trained and ready. You just turn them on.</p>
        </div>

        <div className="mt-10 flex flex-wrap gap-2 justify-center sticky top-16 bg-white py-4 z-10 border-b border-border">
          {departments.map((d) => (
            <button key={d.name} onClick={() => setActive(d.name)}
              className={`px-4 py-2 rounded-full text-sm font-medium transition-colors ${active === d.name ? "bg-primary text-white" : "bg-bg-secondary text-text-secondary hover:bg-gray-200"}`}>
              {d.name}
            </button>
          ))}
        </div>

        <div className="mt-8 grid md:grid-cols-2 gap-6">
          {departments.find((d) => d.name === active)?.agents.map((agent) => (
            <Card key={agent.name}>
              <CardContent className="p-6">
                <div className="flex items-start justify-between">
                  <h3 className="text-lg font-semibold text-text-primary">{agent.name}</h3>
                  <Badge variant={modelColor[agent.model as keyof typeof modelColor]}>{agent.model}</Badge>
                </div>
                <p className="mt-2 text-sm text-text-secondary">{agent.desc}</p>
                <p className="mt-3 text-xs text-text-secondary">Trigger: <span className="font-medium">{agent.trigger}</span></p>
              </CardContent>
            </Card>
          ))}
        </div>

        {/* Comparison Table */}
        <div className="mt-20">
          <h2 className="text-2xl font-bold text-center text-text-primary">ARIA vs the alternatives</h2>
          <div className="mt-8 overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-border">
                <th className="text-left py-3 px-4 font-medium text-text-secondary">Feature</th>
                <th className="text-center py-3 px-4 font-medium text-primary">ARIA</th>
                <th className="text-center py-3 px-4 font-medium text-text-secondary">Hiring Staff</th>
                <th className="text-center py-3 px-4 font-medium text-text-secondary">Zapier</th>
                <th className="text-center py-3 px-4 font-medium text-text-secondary">HubSpot</th>
              </tr></thead>
              <tbody>
                {[
                  ["Monthly cost", "$79-399", "$5,000+", "$20-600", "$50-3,600"],
                  ["Setup time", "15 minutes", "Weeks", "Hours-days", "Days-weeks"],
                  ["24/7 availability", "✓", "✗", "✓", "Partial"],
                  ["Business knowledge", "Deep", "Varies", "None", "Limited"],
                  ["Departments covered", "All 6", "1-2 per hire", "Limited", "Sales only"],
                ].map(([feature, ...values]) => (
                  <tr key={feature} className="border-b border-border">
                    <td className="py-3 px-4 text-text-primary font-medium">{feature}</td>
                    {values.map((v, i) => <td key={i} className={`py-3 px-4 text-center ${i === 0 ? "text-primary font-medium" : "text-text-secondary"}`}>{v}</td>)}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    </div>
  );
}
