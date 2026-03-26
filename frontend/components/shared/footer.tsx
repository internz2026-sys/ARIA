import Link from "next/link";

const columns = [
  { title: "Product", links: [{ label: "Features", href: "/features" }, { label: "Pricing", href: "/pricing" }, { label: "Use Cases", href: "/use-cases" }, { label: "Agents", href: "/features" }] },
  { title: "Company", links: [{ label: "About", href: "/about" }, { label: "Blog", href: "/blog" }, { label: "Careers", href: "/about" }, { label: "Contact", href: "/contact" }] },
  { title: "Resources", links: [{ label: "Documentation", href: "#" }, { label: "API", href: "#" }, { label: "Status", href: "#" }, { label: "Changelog", href: "#" }] },
  { title: "Legal", links: [{ label: "Privacy", href: "/privacy" }, { label: "Terms", href: "/terms" }, { label: "Security", href: "/security" }, { label: "GDPR", href: "/gdpr" }] },
];

export function Footer() {
  return (
    <footer className="border-t border-border bg-bg-secondary">
      <div className="mx-auto max-w-7xl px-4 py-12 sm:px-6 lg:px-8">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-8">
          <div className="col-span-2 md:col-span-1">
            <span className="text-2xl font-bold text-primary">ARIA</span>
            <p className="mt-2 text-sm text-text-secondary">AI Agents That Run Your Business</p>
          </div>
          {columns.map((col) => (
            <div key={col.title}>
              <h4 className="text-sm font-semibold text-text-primary">{col.title}</h4>
              <ul className="mt-3 space-y-2">
                {col.links.map((link) => (
                  <li key={link.label}>
                    <Link href={link.href} className="text-sm text-text-secondary hover:text-primary transition-colors">{link.label}</Link>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
        <div className="mt-8 border-t border-border pt-8 flex flex-col sm:flex-row justify-between items-center gap-4">
          <p className="text-xs text-text-secondary">Made for businesses worldwide. &copy; {new Date().getFullYear()} ARIA. All rights reserved.</p>
        </div>
      </div>
    </footer>
  );
}
