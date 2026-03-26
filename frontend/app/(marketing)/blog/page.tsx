import Link from "next/link";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

const posts = [
  { slug: "automate-small-business", category: "Automation guides", title: "How to Automate 80% of Your Small Business Operations", excerpt: "A practical guide to identifying which tasks to automate first and how AI agents can handle them.", author: "ARIA Team", readTime: "6 min", date: "Mar 20, 2026", featured: true },
  { slug: "lead-gen-agents", category: "Agent deep-dives", title: "How the Lead Gen Agent Finds Your Ideal Customers", excerpt: "Under the hood of ARIA's lead gen agent — ICP matching, enrichment, and scoring.", author: "ARIA Team", readTime: "5 min", date: "Mar 18, 2026" },
  { slug: "restaurant-automation", category: "Case studies", title: "How Bella Cucina Tripled Their Google Reviews with ARIA", excerpt: "A 3-location restaurant chain went from 12 to 47 reviews per month using automated follow-ups.", author: "ARIA Team", readTime: "4 min", date: "Mar 15, 2026" },
  { slug: "ai-vs-virtual-assistant", category: "Automation guides", title: "AI Agents vs Virtual Assistants: Which Is Right for You?", excerpt: "Comparing cost, reliability, and capability between human VAs and AI-powered automation.", author: "ARIA Team", readTime: "7 min", date: "Mar 12, 2026" },
  { slug: "closer-agent-deep-dive", category: "Agent deep-dives", title: "The Closer Agent: How AI Handles Sales Objections", excerpt: "How ARIA's most sophisticated agent uses Claude Opus to close deals.", author: "ARIA Team", readTime: "8 min", date: "Mar 10, 2026" },
  { slug: "whats-new-march", category: "Product updates", title: "What's New in ARIA — March 2026", excerpt: "New WhatsApp integration, improved analytics dashboard, and 3 new agent capabilities.", author: "ARIA Team", readTime: "3 min", date: "Mar 1, 2026" },
];

export default function BlogPage() {
  const featured = posts[0];
  const rest = posts.slice(1);

  return (
    <div className="py-16">
      <div className="mx-auto max-w-7xl px-4">
        <h1 className="text-4xl font-bold text-text-primary">The ARIA Blog</h1>
        <p className="mt-2 text-text-secondary">Automation guides for business owners</p>

        {/* Featured */}
        <Card className="mt-8">
          <CardContent className="p-8">
            <Badge>{featured.category}</Badge>
            <h2 className="mt-3 text-2xl font-bold text-text-primary">{featured.title}</h2>
            <p className="mt-2 text-text-secondary">{featured.excerpt}</p>
            <div className="mt-4 flex items-center gap-4 text-sm text-text-secondary">
              <span>{featured.author}</span><span>·</span><span>{featured.readTime} read</span><span>·</span><span>{featured.date}</span>
            </div>
          </CardContent>
        </Card>

        {/* Grid */}
        <div className="mt-10 grid md:grid-cols-2 gap-6">
          {rest.map((post) => (
            <Card key={post.slug}>
              <CardContent className="p-6">
                <Badge variant="secondary">{post.category}</Badge>
                <h3 className="mt-3 text-lg font-semibold text-text-primary">{post.title}</h3>
                <p className="mt-2 text-sm text-text-secondary">{post.excerpt}</p>
                <div className="mt-3 flex items-center gap-3 text-xs text-text-secondary">
                  <span>{post.author}</span><span>·</span><span>{post.readTime}</span><span>·</span><span>{post.date}</span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
}
