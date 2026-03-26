"use client";
import { useState } from "react";
import Link from "next/link";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";

const filters = ["All", "Restaurant", "Agency", "E-commerce", "Consultant", "Clinic", "SaaS"];

const useCases = [
  {
    type: "Restaurant", title: "Restaurant — 3 Branches", desc: "Multi-location restaurant with catering services.",
    agents: ["Follow-up agent", "Catering inquiry agent", "Review response agent", "Accounting summary agent", "Staff payroll agent"],
    quote: { text: "No-shows dropped by 60% and our Google reviews tripled.", name: "Marco R.", company: "Bella Cucina" },
  },
  {
    type: "Agency", title: "Web Design Agency — 5-person B2B", desc: "B2B agency serving SME clients with web design services.",
    agents: ["Lead gen agent", "Outreach agent", "Follow-up agent", "Invoice chasing agent", "Referral agent"],
    quote: { text: "ARIA books 3x more discovery calls than our old process.", name: "Lisa K.", company: "PixelCraft Studio" },
  },
  {
    type: "E-commerce", title: "E-Commerce — Shopify / Instagram", desc: "Global skincare brand selling on Shopify and Instagram.",
    agents: ["Abandoned cart agent", "Post-purchase upsell agent", "Instagram DM agent", "Review collector agent", "Inventory alert agent"],
    quote: { text: "Cart recovery went from 5% to 22%. ARIA pays for itself 10x over.", name: "Priya S.", company: "GlowNaturals" },
  },
  {
    type: "Consultant", title: "Solo Consultant — Professional Services", desc: "Independent HR consultant working with mid-size companies.",
    agents: ["LinkedIn lead gen agent", "Outreach agent", "Closer agent", "Invoice agent", "Content agent"],
    quote: { text: "I went from spending 40% of my time on admin to under 5%.", name: "David T.", company: "TalentEdge Consulting" },
  },
  {
    type: "Clinic", title: "Dental Clinic — Local Service", desc: "General and cosmetic dentistry practice with 2 dentists.",
    agents: ["Appointment reminder agent", "No-show follow-up agent", "Review request agent", "Re-engagement agent", "Monthly report agent"],
    quote: { text: "Patient retention improved by 35% in the first quarter.", name: "Dr. Amy L.", company: "Smile Dental Clinic" },
  },
  {
    type: "SaaS", title: "SaaS Startup — B2B Software", desc: "12-person company selling project management software to construction.",
    agents: ["Lead gen agent", "Outreach agent", "Closer agent", "Onboarding agent", "Churn detection agent", "Analytics agent"],
    quote: { text: "Our trial-to-paid conversion jumped from 8% to 19%.", name: "Chris M.", company: "BuildFlow" },
  },
];

export default function UseCasesPage() {
  const [filter, setFilter] = useState("All");
  const filtered = filter === "All" ? useCases : useCases.filter((u) => u.type === filter);

  return (
    <div className="py-16">
      <div className="mx-auto max-w-7xl px-4">
        <div className="text-center">
          <h1 className="text-4xl font-bold text-text-primary">ARIA works for every business type</h1>
          <p className="mt-3 text-text-secondary">See how ARIA auto-configures for your industry.</p>
        </div>

        <div className="mt-8 flex flex-wrap gap-2 justify-center">
          {filters.map((f) => (
            <button key={f} onClick={() => setFilter(f)}
              className={`px-4 py-2 rounded-full text-sm font-medium transition-colors ${filter === f ? "bg-primary text-white" : "bg-bg-secondary text-text-secondary hover:bg-gray-200"}`}>
              {f}
            </button>
          ))}
        </div>

        <div className="mt-12 grid md:grid-cols-2 gap-8">
          {filtered.map((uc) => (
            <Card key={uc.title}>
              <CardContent className="p-6">
                <h3 className="text-xl font-semibold text-text-primary">{uc.title}</h3>
                <p className="mt-1 text-sm text-text-secondary">{uc.desc}</p>
                <div className="mt-4">
                  <p className="text-xs font-medium text-primary uppercase tracking-wide">ARIA auto-configures:</p>
                  <ul className="mt-2 space-y-1">
                    {uc.agents.map((a) => <li key={a} className="flex items-center gap-2 text-sm text-text-secondary"><span className="text-success">✓</span>{a}</li>)}
                  </ul>
                </div>
                <div className="mt-4 p-3 bg-bg-secondary rounded-input">
                  <p className="text-sm text-text-secondary italic">&ldquo;{uc.quote.text}&rdquo;</p>
                  <p className="mt-1 text-xs font-medium text-text-primary">{uc.quote.name}, {uc.quote.company}</p>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>

        <div className="mt-16 text-center p-8 bg-primary-light rounded-card">
          <h3 className="text-xl font-semibold text-text-primary">Don&apos;t see your business type?</h3>
          <p className="mt-2 text-text-secondary">ARIA works for any business. Describe yours and we&apos;ll configure it.</p>
          <Button className="mt-4" asChild><Link href="/signup">Try ARIA free</Link></Button>
        </div>
      </div>
    </div>
  );
}
