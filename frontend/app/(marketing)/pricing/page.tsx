"use client";
import { useState } from "react";
import Link from "next/link";
import { Check, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

const plans = [
  { name: "Starter", monthly: 79, annual: 63, tagline: "For freelancers and solo owners", agents: "Any 3 agents", features: ["50 leads/day", "100 emails/day", "1 channel", "Email support", "Basic analytics"] },
  { name: "Growth", monthly: 199, annual: 159, tagline: "For SMEs and growing teams", agents: "Any 8 agents", features: ["200 leads/day", "500 emails/day", "3 channels", "Priority support", "Advanced analytics", "Custom ICP"], popular: true },
  { name: "Business", monthly: 399, annual: 319, tagline: "For established businesses", agents: "All 18 agents", features: ["Unlimited leads & emails", "All channels", "Priority support", "Custom reports", "API access", "Team management"] },
  { name: "Scale", monthly: 799, annual: 639, tagline: "For multi-location & enterprise", agents: "All 18 + custom", features: ["Everything in Business", "Dedicated CSM", "SLA guarantee", "White-label option", "Custom integrations", "Onboarding support"] },
];

export default function PricingPage() {
  const [annual, setAnnual] = useState(false);

  return (
    <div className="py-16">
      <div className="mx-auto max-w-7xl px-4">
        <div className="text-center">
          <h1 className="text-4xl font-bold text-text-primary">Simple, honest pricing</h1>
          <p className="mt-3 text-text-secondary">14-day free trial on all plans. No credit card required to start.</p>

          <div className="mt-8 inline-flex items-center gap-3 bg-bg-secondary rounded-full p-1">
            <button onClick={() => setAnnual(false)} className={`px-4 py-2 rounded-full text-sm font-medium transition-colors ${!annual ? "bg-white text-primary shadow-sm" : "text-text-secondary"}`}>Monthly</button>
            <button onClick={() => setAnnual(true)} className={`px-4 py-2 rounded-full text-sm font-medium transition-colors ${annual ? "bg-white text-primary shadow-sm" : "text-text-secondary"}`}>Annual (save 20%)</button>
          </div>
        </div>

        <div className="mt-12 grid md:grid-cols-2 lg:grid-cols-4 gap-6">
          {plans.map((plan) => (
            <Card key={plan.name} className={plan.popular ? "border-2 border-primary relative" : ""}>
              {plan.popular && <div className="absolute -top-3 left-1/2 -translate-x-1/2 bg-primary text-white text-xs font-medium px-3 py-1 rounded-full">Most popular</div>}
              <CardContent className="p-6">
                <h3 className="text-lg font-semibold text-text-primary">{plan.name}</h3>
                <p className="text-xs text-text-secondary mt-1">{plan.tagline}</p>
                <p className="mt-4 text-4xl font-bold text-text-primary">
                  ${annual ? plan.annual : plan.monthly}<span className="text-sm font-normal text-text-secondary">/mo</span>
                </p>
                {annual && <p className="text-xs text-success">Save ${(plan.monthly - plan.annual) * 12}/year</p>}
                <p className="mt-3 text-sm font-medium text-primary">{plan.agents}</p>
                <ul className="mt-4 space-y-2">
                  {plan.features.map((f) => (
                    <li key={f} className="flex items-center gap-2 text-sm text-text-secondary">
                      <Check className="h-4 w-4 text-success flex-shrink-0" />{f}
                    </li>
                  ))}
                </ul>
                <Button className="w-full mt-6" variant={plan.popular ? "default" : "outline"} asChild>
                  <Link href="/signup">Start free trial</Link>
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>

        {/* Add-ons */}
        <div className="mt-16 text-center">
          <h2 className="text-2xl font-bold text-text-primary">Add-ons</h2>
          <div className="mt-6 grid sm:grid-cols-3 gap-6 max-w-2xl mx-auto">
            {[
              { name: "Extra agents", price: "$20/agent/mo", desc: "Add agents beyond your plan limit" },
              { name: "Additional channels", price: "$30/channel/mo", desc: "Connect more communication channels" },
              { name: "White-label", price: "+$200/mo", desc: "Remove ARIA branding entirely" },
            ].map((addon) => (
              <Card key={addon.name}>
                <CardContent className="p-4 text-center">
                  <p className="font-semibold text-text-primary">{addon.name}</p>
                  <p className="text-primary font-bold mt-1">{addon.price}</p>
                  <p className="text-xs text-text-secondary mt-1">{addon.desc}</p>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
