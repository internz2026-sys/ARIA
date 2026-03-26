"use client";
import Link from "next/link";
import { useState } from "react";
import { Menu, X } from "lucide-react";
import { Button } from "@/components/ui/button";

const navLinks = [
  { href: "/features", label: "Features" },
  { href: "/pricing", label: "Pricing" },
  { href: "/use-cases", label: "Use Cases" },
  { href: "/blog", label: "Blog" },
];

export function Navbar() {
  const [mobileOpen, setMobileOpen] = useState(false);

  return (
    <nav className="sticky top-0 z-50 border-b border-border bg-white/95 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
        <Link href="/" className="text-2xl font-bold text-primary">ARIA</Link>

        <div className="hidden md:flex items-center gap-8">
          {navLinks.map((link) => (
            <Link key={link.href} href={link.href} className="text-sm font-medium text-text-secondary hover:text-primary transition-colors">
              {link.label}
            </Link>
          ))}
        </div>

        <div className="hidden md:flex items-center gap-3">
          <Button variant="ghost" size="sm" asChild><Link href="/login">Sign in</Link></Button>
          <Button size="sm" asChild><Link href="/signup">Get started</Link></Button>
        </div>

        <button className="md:hidden" onClick={() => setMobileOpen(!mobileOpen)}>
          {mobileOpen ? <X className="h-6 w-6" /> : <Menu className="h-6 w-6" />}
        </button>
      </div>

      {mobileOpen && (
        <div className="md:hidden border-t border-border bg-white px-4 py-4 space-y-3">
          {navLinks.map((link) => (
            <Link key={link.href} href={link.href} className="block text-sm font-medium text-text-secondary py-2" onClick={() => setMobileOpen(false)}>
              {link.label}
            </Link>
          ))}
          <div className="flex gap-3 pt-3">
            <Button variant="outline" size="sm" className="flex-1" asChild><Link href="/login">Sign in</Link></Button>
            <Button size="sm" className="flex-1" asChild><Link href="/signup">Get started</Link></Button>
          </div>
        </div>
      )}
    </nav>
  );
}
