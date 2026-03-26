import { Card, CardContent } from "@/components/ui/card";
import { Lightbulb, Heart, Shield, Zap } from "lucide-react";

const team = [
  { name: "Alex Rivera", role: "CEO & Co-founder", bio: "Former ops lead at a Series B startup. Built ARIA after watching SME clients struggle with tool complexity." },
  { name: "Priya Sharma", role: "CTO & Co-founder", bio: "AI engineer with 10 years in NLP. Previously built conversational AI systems at scale." },
  { name: "James Okonkwo", role: "Head of Product", bio: "Product leader who spent 5 years building automation tools for non-technical users." },
  { name: "Maria Santos", role: "Head of Growth", bio: "Growth marketer who scaled two SaaS products from 0 to 10K customers." },
];

const values = [
  { icon: Lightbulb, title: "Simplicity first", desc: "If an owner can't set it up in 15 minutes, we haven't done our job." },
  { icon: Heart, title: "Human empathy", desc: "Our agents speak like humans, not robots. Empathy is built into every prompt." },
  { icon: Shield, title: "Trust & transparency", desc: "Owners always see what their agents are doing. No black boxes." },
  { icon: Zap, title: "Immediate value", desc: "Every feature ships working. No 'coming soon.' No empty dashboards." },
];

export default function AboutPage() {
  return (
    <div className="py-16">
      <div className="mx-auto max-w-7xl px-4">
        <div className="text-center max-w-3xl mx-auto">
          <h1 className="text-4xl font-bold text-text-primary">We believe every business owner deserves an AI team.</h1>
          <div className="mt-8 text-left space-y-4 text-text-secondary">
            <p>ARIA was born from a simple observation: small business owners are the hardest-working people on the planet, yet they spend 40% of their time on tasks that a well-configured AI could handle in seconds.</p>
            <p>We watched restaurant owners manually chase reviews, agency founders write the same follow-up emails over and over, and consultants lose deals because they forgot to follow up. The tools existed to help — but they were built for enterprises with dedicated ops teams, not for a solo founder juggling everything.</p>
            <p>So we built ARIA: a platform where you describe your business once, and AI agents handle the rest. No coding. No configuration wizards. No 200-page documentation. Just tell us what you do, and we set up your entire AI team in 15 minutes.</p>
          </div>
        </div>

        <div className="mt-16">
          <h2 className="text-2xl font-bold text-center text-text-primary">Our team</h2>
          <div className="mt-8 grid sm:grid-cols-2 lg:grid-cols-4 gap-6">
            {team.map((member) => (
              <Card key={member.name}>
                <CardContent className="p-6 text-center">
                  <div className="mx-auto h-16 w-16 rounded-full bg-primary-light flex items-center justify-center text-primary text-xl font-bold">{member.name[0]}</div>
                  <h3 className="mt-3 font-semibold text-text-primary">{member.name}</h3>
                  <p className="text-xs text-primary font-medium">{member.role}</p>
                  <p className="mt-2 text-sm text-text-secondary">{member.bio}</p>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>

        <div className="mt-16">
          <h2 className="text-2xl font-bold text-center text-text-primary">Our values</h2>
          <div className="mt-8 grid sm:grid-cols-2 gap-6 max-w-3xl mx-auto">
            {values.map((v) => (
              <div key={v.title} className="flex gap-4">
                <v.icon className="h-8 w-8 text-primary flex-shrink-0" />
                <div>
                  <h3 className="font-semibold text-text-primary">{v.title}</h3>
                  <p className="mt-1 text-sm text-text-secondary">{v.desc}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
