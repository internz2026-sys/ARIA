"use client";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Mail, Headphones, Newspaper } from "lucide-react";

const contacts = [
  { icon: Mail, title: "Sales", desc: "Enterprise inquiries and custom plans", email: "sales@aria.ai" },
  { icon: Headphones, title: "Support", desc: "Help with your ARIA account", email: "support@aria.ai" },
  { icon: Newspaper, title: "Press", desc: "Media inquiries and partnerships", email: "press@aria.ai" },
];

export default function ContactPage() {
  return (
    <div className="py-16">
      <div className="mx-auto max-w-7xl px-4">
        <div className="text-center">
          <h1 className="text-4xl font-bold text-text-primary">Get in touch</h1>
          <p className="mt-3 text-text-secondary">We&apos;d love to hear from you.</p>
        </div>

        <div className="mt-12 grid lg:grid-cols-2 gap-12">
          <Card>
            <CardContent className="p-8 space-y-4">
              <Input label="Full name" placeholder="Your name" />
              <Input label="Email" type="email" placeholder="you@company.com" />
              <Input label="Company" placeholder="Your company name" />
              <div className="space-y-1">
                <label className="text-sm font-medium text-text-primary">Inquiry type</label>
                <select className="flex h-10 w-full rounded-input border border-border bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary/20">
                  <option>Sales</option><option>Support</option><option>Press</option><option>Partnership</option>
                </select>
              </div>
              <div className="space-y-1">
                <label className="text-sm font-medium text-text-primary">Message</label>
                <textarea className="flex min-h-[120px] w-full rounded-input border border-border bg-white px-3 py-2 text-sm placeholder:text-text-secondary/50 focus:outline-none focus:ring-2 focus:ring-primary/20" placeholder="Tell us how we can help..." />
              </div>
              <Button className="w-full">Send message</Button>
            </CardContent>
          </Card>

          <div className="space-y-6">
            {contacts.map((c) => (
              <Card key={c.title}>
                <CardContent className="p-6 flex items-start gap-4">
                  <c.icon className="h-8 w-8 text-primary flex-shrink-0" />
                  <div>
                    <h3 className="font-semibold text-text-primary">{c.title}</h3>
                    <p className="text-sm text-text-secondary">{c.desc}</p>
                    <p className="mt-1 text-sm text-primary font-medium">{c.email}</p>
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
