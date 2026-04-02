"use client";

import { useState, useEffect, useRef } from "react";
import Link from "next/link";

const FONTS_LINK =
  "https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=JetBrains+Mono:wght@400;500;600&family=Sora:wght@300;400;500;600;700&display=swap";

function loadFonts() {
  if (typeof document === "undefined") return;
  if (!document.querySelector(`link[href*="Instrument+Serif"]`)) {
    const link = document.createElement("link");
    link.rel = "stylesheet";
    link.href = FONTS_LINK;
    document.head.appendChild(link);
  }
}

// Intersection Observer hook
function useInView(threshold = 0.15): [React.RefObject<HTMLDivElement>, boolean] {
  const ref = useRef<HTMLDivElement>(null!);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const obs = new IntersectionObserver(
      ([e]) => {
        if (e.isIntersecting) setInView(true);
      },
      { threshold }
    );
    obs.observe(el);
    return () => obs.disconnect();
  }, [threshold]);
  return [ref, inView];
}

// Typewriter effect
function Typewriter({ text, speed = 40, delay = 0 }: { text: string; speed?: number; delay?: number }) {
  const [displayed, setDisplayed] = useState("");
  const [started, setStarted] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setStarted(true), delay);
    return () => clearTimeout(t);
  }, [delay]);
  useEffect(() => {
    if (!started) return;
    let i = 0;
    const iv = setInterval(() => {
      i++;
      setDisplayed(text.slice(0, i));
      if (i >= text.length) clearInterval(iv);
    }, speed);
    return () => clearInterval(iv);
  }, [started, text, speed]);
  return (
    <span>
      {displayed}
      <span style={{ opacity: displayed.length < text.length ? 1 : 0, transition: "opacity 0.3s" }}>|</span>
    </span>
  );
}

// Agent node component
function AgentNode({ name, role, color, delay, active }: { name: string; role: string; color: string; delay: number; active: boolean }) {
  return (
    <div
      style={{
        background: "rgba(255,255,255,0.03)",
        border: `1px solid ${active ? color : "rgba(255,255,255,0.08)"}`,
        borderRadius: 12,
        padding: "20px 24px",
        minWidth: 180,
        opacity: active ? 1 : 0,
        transform: active ? "translateY(0)" : "translateY(20px)",
        transition: `all 0.6s cubic-bezier(0.16, 1, 0.3, 1) ${delay}ms`,
        position: "relative" as const,
      }}
    >
      <div
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          background: color,
          position: "absolute" as const,
          top: 12,
          right: 12,
          boxShadow: `0 0 12px ${color}80`,
        }}
      />
      <div style={{ fontFamily: "'JetBrains Mono'", fontSize: 13, color, marginBottom: 4, letterSpacing: 0.5 }}>{role}</div>
      <div style={{ fontFamily: "'Sora'", fontSize: 16, fontWeight: 600, color: "#F0EDE8" }}>{name}</div>
    </div>
  );
}

// Feature card
function FeatureCard({ icon, title, description, index, inView }: { icon: string; title: string; description: string; index: number; inView: boolean }) {
  return (
    <div
      style={{
        background: "rgba(255,255,255,0.02)",
        border: "1px solid rgba(255,255,255,0.06)",
        borderRadius: 16,
        padding: "36px 32px",
        opacity: inView ? 1 : 0,
        transform: inView ? "translateY(0)" : "translateY(30px)",
        transition: `all 0.7s cubic-bezier(0.16, 1, 0.3, 1) ${index * 120}ms`,
      }}
    >
      <div style={{ fontSize: 32, marginBottom: 16 }}>{icon}</div>
      <h3 style={{ fontFamily: "'Sora'", fontSize: 20, fontWeight: 600, color: "#F0EDE8", marginBottom: 12, lineHeight: 1.3 }}>{title}</h3>
      <p style={{ fontFamily: "'Sora'", fontSize: 15, color: "rgba(240,237,232,0.55)", lineHeight: 1.7, margin: 0 }}>{description}</p>
    </div>
  );
}

// Pricing card
function PricingCard({ tier, price, features, highlighted, index, inView }: { tier: string; price: string; features: string[]; highlighted: boolean; index: number; inView: boolean }) {
  return (
    <div
      style={{
        background: highlighted ? "rgba(233,69,96,0.06)" : "rgba(255,255,255,0.02)",
        border: `1px solid ${highlighted ? "rgba(233,69,96,0.3)" : "rgba(255,255,255,0.06)"}`,
        borderRadius: 16,
        padding: "40px 32px",
        position: "relative" as const,
        opacity: inView ? 1 : 0,
        transform: inView ? "translateY(0)" : "translateY(30px)",
        transition: `all 0.7s cubic-bezier(0.16, 1, 0.3, 1) ${index * 150}ms`,
        flex: "1 1 0",
        minWidth: 260,
      }}
    >
      {highlighted && (
        <div
          style={{
            position: "absolute" as const,
            top: -1,
            left: 40,
            right: 40,
            height: 2,
            background: "linear-gradient(90deg, transparent, #E94560, transparent)",
          }}
        />
      )}
      <div style={{ fontFamily: "'JetBrains Mono'", fontSize: 12, color: highlighted ? "#E94560" : "rgba(240,237,232,0.4)", letterSpacing: 2, textTransform: "uppercase" as const, marginBottom: 12 }}>{tier}</div>
      <div style={{ fontFamily: "'Sora'", fontSize: 44, fontWeight: 700, color: "#F0EDE8", marginBottom: 4 }}>{price}</div>
      <div style={{ fontFamily: "'Sora'", fontSize: 14, color: "rgba(240,237,232,0.35)", marginBottom: 28 }}>per month</div>
      <div style={{ display: "flex", flexDirection: "column" as const, gap: 14 }}>
        {features.map((f, i) => (
          <div key={i} style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
            <span style={{ color: highlighted ? "#E94560" : "rgba(240,237,232,0.3)", fontSize: 14, marginTop: 2, flexShrink: 0 }}>&rarr;</span>
            <span style={{ fontFamily: "'Sora'", fontSize: 14, color: "rgba(240,237,232,0.6)", lineHeight: 1.5 }}>{f}</span>
          </div>
        ))}
      </div>
      <Link href="/signup">
        <button
          style={{
            marginTop: 32,
            width: "100%",
            padding: "14px 0",
            background: highlighted ? "#E94560" : "transparent",
            border: highlighted ? "none" : "1px solid rgba(255,255,255,0.15)",
            borderRadius: 8,
            fontFamily: "'Sora'",
            fontSize: 15,
            fontWeight: 500,
            color: "#F0EDE8",
            cursor: "pointer",
            transition: "all 0.3s ease",
          }}
          onMouseEnter={(e) => {
            (e.target as HTMLButtonElement).style.transform = "translateY(-1px)";
            (e.target as HTMLButtonElement).style.boxShadow = highlighted ? "0 8px 30px rgba(233,69,96,0.3)" : "0 4px 20px rgba(255,255,255,0.05)";
          }}
          onMouseLeave={(e) => {
            (e.target as HTMLButtonElement).style.transform = "";
            (e.target as HTMLButtonElement).style.boxShadow = "";
          }}
        >
          Get Started
        </button>
      </Link>
    </div>
  );
}

// Step component for How It Works
function Step({ number, title, desc, index, inView }: { number: string; title: string; desc: string; index: number; inView: boolean }) {
  return (
    <div
      style={{
        display: "flex",
        gap: 24,
        alignItems: "flex-start",
        opacity: inView ? 1 : 0,
        transform: inView ? "translateX(0)" : "translateX(-30px)",
        transition: `all 0.7s cubic-bezier(0.16, 1, 0.3, 1) ${index * 200}ms`,
      }}
    >
      <div
        style={{
          fontFamily: "'Instrument Serif'",
          fontSize: 56,
          fontStyle: "italic",
          color: "rgba(233,69,96,0.25)",
          lineHeight: 1,
          flexShrink: 0,
          width: 60,
          textAlign: "center" as const,
        }}
      >
        {number}
      </div>
      <div>
        <h4 style={{ fontFamily: "'Sora'", fontSize: 18, fontWeight: 600, color: "#F0EDE8", marginBottom: 8, marginTop: 8 }}>{title}</h4>
        <p style={{ fontFamily: "'Sora'", fontSize: 15, color: "rgba(240,237,232,0.5)", lineHeight: 1.7, margin: 0 }}>{desc}</p>
      </div>
    </div>
  );
}

export default function AriaLanding() {
  useEffect(() => {
    loadFonts();
  }, []);
  const [loaded, setLoaded] = useState(false);
  const [agentsVisible, setAgentsVisible] = useState(false);
  useEffect(() => {
    setTimeout(() => setLoaded(true), 100);
  }, []);

  const [agentRef, agentInView] = useInView(0.2);
  const [featRef, featInView] = useInView(0.1);
  const [howRef, howInView] = useInView(0.1);
  const [priceRef, priceInView] = useInView(0.1);
  const [ctaRef, ctaInView] = useInView(0.2);

  useEffect(() => {
    if (agentInView) setTimeout(() => setAgentsVisible(true), 200);
  }, [agentInView]);

  const features = [
    { icon: "\uD83E\uDDED", title: "GTM Strategy Builder", description: "ARIA interviews you about your product, audience, and goals \u2014 then generates a complete go-to-market playbook. Not generic advice. A specific, actionable plan." },
    { icon: "\u270D\uFE0F", title: "Content That Knows You", description: "Blog posts, landing pages, Product Hunt copy, Show HN posts \u2014 all grounded in your product context and brand voice. No more re-explaining your product to ChatGPT." },
    { icon: "\uD83D\uDCE7", title: "Email Sequences", description: "Welcome series, launch campaigns, newsletters. Complete with subject lines, send timing, and A/B variants. Copy into ConvertKit or Mailchimp and send." },
    { icon: "\uD83D\uDCF1", title: "Social Media Calendar", description: "Platform-optimized posts for X, LinkedIn, and Facebook. Adapted from your content, scheduled on a calendar, ready to copy and post." },
    { icon: "\uD83C\uDFAF", title: "Facebook Ads Playbook", description: "Complete campaign plans with targeting specs, ad copy variants, budget recommendations, and step-by-step Ads Manager instructions. Built for people who've never run an ad." },
    { icon: "\uD83D\uDCCA", title: "Strategy That Adapts", description: "Report your results back to ARIA. It adjusts the playbook, suggests new angles, and tells you what to double down on. Your strategy gets smarter over time." },
  ];

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#0A0A0F",
        color: "#F0EDE8",
        overflowX: "hidden",
      }}
    >
      {/* Noise overlay */}
      <div
        style={{
          position: "fixed",
          inset: 0,
          pointerEvents: "none",
          zIndex: 1,
          opacity: 0.03,
          backgroundImage: `url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")`,
        }}
      />

      {/* Gradient orbs */}
      <div
        style={{
          position: "fixed",
          top: -200,
          right: -200,
          width: 600,
          height: 600,
          background: "radial-gradient(circle, rgba(233,69,96,0.08) 0%, transparent 70%)",
          pointerEvents: "none",
          zIndex: 0,
        }}
      />
      <div
        style={{
          position: "fixed",
          bottom: -300,
          left: -200,
          width: 800,
          height: 800,
          background: "radial-gradient(circle, rgba(15,52,96,0.15) 0%, transparent 70%)",
          pointerEvents: "none",
          zIndex: 0,
        }}
      />

      <div style={{ position: "relative", zIndex: 2 }}>
        {/* Nav */}
        <nav
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            padding: "24px 48px",
            maxWidth: 1200,
            margin: "0 auto",
            opacity: loaded ? 1 : 0,
            transform: loaded ? "translateY(0)" : "translateY(-10px)",
            transition: "all 0.8s cubic-bezier(0.16, 1, 0.3, 1)",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <img src="/logo.webp" alt="ARIA" style={{ height: 32, width: "auto" }} />
          </div>
          <div style={{ display: "flex", gap: 36, alignItems: "center" }}>
            <a
              href="#features"
              style={{ fontFamily: "'Sora'", fontSize: 14, color: "rgba(240,237,232,0.5)", textDecoration: "none", transition: "color 0.2s" }}
              onMouseEnter={(e) => ((e.target as HTMLAnchorElement).style.color = "#F0EDE8")}
              onMouseLeave={(e) => ((e.target as HTMLAnchorElement).style.color = "rgba(240,237,232,0.5)")}
            >
              Features
            </a>
            <a
              href="#how"
              style={{ fontFamily: "'Sora'", fontSize: 14, color: "rgba(240,237,232,0.5)", textDecoration: "none", transition: "color 0.2s" }}
              onMouseEnter={(e) => ((e.target as HTMLAnchorElement).style.color = "#F0EDE8")}
              onMouseLeave={(e) => ((e.target as HTMLAnchorElement).style.color = "rgba(240,237,232,0.5)")}
            >
              How It Works
            </a>
            <a
              href="#pricing"
              style={{ fontFamily: "'Sora'", fontSize: 14, color: "rgba(240,237,232,0.5)", textDecoration: "none", transition: "color 0.2s" }}
              onMouseEnter={(e) => ((e.target as HTMLAnchorElement).style.color = "#F0EDE8")}
              onMouseLeave={(e) => ((e.target as HTMLAnchorElement).style.color = "rgba(240,237,232,0.5)")}
            >
              Pricing
            </a>
            <Link href="/signup">
              <button
                style={{
                  fontFamily: "'Sora'",
                  fontSize: 14,
                  fontWeight: 500,
                  background: "#E94560",
                  border: "none",
                  borderRadius: 8,
                  color: "#fff",
                  padding: "10px 24px",
                  cursor: "pointer",
                  transition: "all 0.3s ease",
                }}
                onMouseEnter={(e) => {
                  (e.target as HTMLButtonElement).style.boxShadow = "0 6px 24px rgba(233,69,96,0.4)";
                  (e.target as HTMLButtonElement).style.transform = "translateY(-1px)";
                }}
                onMouseLeave={(e) => {
                  (e.target as HTMLButtonElement).style.boxShadow = "";
                  (e.target as HTMLButtonElement).style.transform = "";
                }}
              >
                Join Waitlist
              </button>
            </Link>
          </div>
        </nav>

        {/* Hero */}
        <section style={{ maxWidth: 1200, margin: "0 auto", padding: "100px 48px 80px", textAlign: "center" as const }}>
          <div
            style={{
              fontFamily: "'JetBrains Mono'",
              fontSize: 13,
              letterSpacing: 3,
              color: "#E94560",
              marginBottom: 32,
              textTransform: "uppercase" as const,
              opacity: loaded ? 1 : 0,
              transition: "opacity 0.8s 0.2s",
            }}
          >
            YOUR AI MARKETING TEAM
          </div>
          <h1
            style={{
              fontFamily: "'Instrument Serif'",
              fontSize: "clamp(48px, 7vw, 84px)",
              fontWeight: 400,
              lineHeight: 1.08,
              margin: "0 auto 32px",
              maxWidth: 900,
              opacity: loaded ? 1 : 0,
              transform: loaded ? "translateY(0)" : "translateY(20px)",
              transition: "all 1s cubic-bezier(0.16, 1, 0.3, 1) 0.3s",
            }}
          >
            You build the product.
            <br />
            <span style={{ fontStyle: "italic", color: "#E94560" }}>ARIA markets it.</span>
          </h1>
          <p
            style={{
              fontFamily: "'Sora'",
              fontSize: 18,
              color: "rgba(240,237,232,0.5)",
              lineHeight: 1.7,
              maxWidth: 600,
              margin: "0 auto 48px",
              fontWeight: 300,
              opacity: loaded ? 1 : 0,
              transition: "opacity 1s 0.6s",
            }}
          >
            ARIA gives developer founders an AI marketing team that builds your GTM strategy and executes it &mdash; content, email, social, and ads &mdash; so you can stop guessing and start growing.
          </p>
          <div
            style={{
              display: "flex",
              gap: 16,
              justifyContent: "center",
              flexWrap: "wrap" as const,
              opacity: loaded ? 1 : 0,
              transition: "opacity 1s 0.8s",
            }}
          >
            <Link href="/signup">
              <button
                style={{
                  fontFamily: "'Sora'",
                  fontSize: 16,
                  fontWeight: 500,
                  background: "#E94560",
                  border: "none",
                  borderRadius: 10,
                  color: "#fff",
                  padding: "16px 40px",
                  cursor: "pointer",
                  transition: "all 0.3s ease",
                }}
                onMouseEnter={(e) => {
                  (e.target as HTMLButtonElement).style.boxShadow = "0 8px 32px rgba(233,69,96,0.4)";
                  (e.target as HTMLButtonElement).style.transform = "translateY(-2px)";
                }}
                onMouseLeave={(e) => {
                  (e.target as HTMLButtonElement).style.boxShadow = "";
                  (e.target as HTMLButtonElement).style.transform = "";
                }}
              >
                Join the Waitlist
              </button>
            </Link>
            <button
              style={{
                fontFamily: "'Sora'",
                fontSize: 16,
                fontWeight: 500,
                background: "transparent",
                border: "1px solid rgba(255,255,255,0.15)",
                borderRadius: 10,
                color: "#F0EDE8",
                padding: "16px 40px",
                cursor: "pointer",
                transition: "all 0.3s ease",
              }}
              onMouseEnter={(e) => {
                (e.target as HTMLButtonElement).style.borderColor = "rgba(255,255,255,0.3)";
                (e.target as HTMLButtonElement).style.transform = "translateY(-2px)";
              }}
              onMouseLeave={(e) => {
                (e.target as HTMLButtonElement).style.borderColor = "rgba(255,255,255,0.15)";
                (e.target as HTMLButtonElement).style.transform = "";
              }}
            >
              Watch Demo
            </button>
          </div>

          {/* Terminal preview */}
          <div
            style={{
              maxWidth: 700,
              margin: "80px auto 0",
              background: "rgba(255,255,255,0.02)",
              border: "1px solid rgba(255,255,255,0.06)",
              borderRadius: 16,
              overflow: "hidden",
              opacity: loaded ? 1 : 0,
              transform: loaded ? "translateY(0)" : "translateY(30px)",
              transition: "all 1s cubic-bezier(0.16, 1, 0.3, 1) 1s",
            }}
          >
            <div
              style={{
                display: "flex",
                gap: 8,
                padding: "16px 20px",
                borderBottom: "1px solid rgba(255,255,255,0.06)",
              }}
            >
              <div style={{ width: 12, height: 12, borderRadius: "50%", background: "rgba(255,255,255,0.08)" }} />
              <div style={{ width: 12, height: 12, borderRadius: "50%", background: "rgba(255,255,255,0.08)" }} />
              <div style={{ width: 12, height: 12, borderRadius: "50%", background: "rgba(255,255,255,0.08)" }} />
              <span style={{ fontFamily: "'JetBrains Mono'", fontSize: 12, color: "rgba(240,237,232,0.25)", marginLeft: 12 }}>aria &mdash; marketing session</span>
            </div>
            <div style={{ padding: "24px 28px", fontFamily: "'JetBrains Mono'", fontSize: 14, lineHeight: 2 }}>
              <div style={{ color: "#E94560" }}>ARIA_CEO &rarr;</div>
              <div style={{ color: "rgba(240,237,232,0.7)", paddingLeft: 16 }}>
                <Typewriter
                  text="I've analyzed your product and audience. Here's your 30-day GTM plan: Week 1 — launch blog + Product Hunt prep. Week 2 — email sequence to beta users. Week 3 — LinkedIn thought leadership push. Week 4 — Facebook ads targeting early adopters. I'll draft everything. Ready?"
                  speed={25}
                  delay={1500}
                />
              </div>
            </div>
          </div>
        </section>

        {/* Social proof bar */}
        <section
          style={{
            borderTop: "1px solid rgba(255,255,255,0.04)",
            borderBottom: "1px solid rgba(255,255,255,0.04)",
            padding: "32px 48px",
            textAlign: "center" as const,
          }}
        >
          <p
            style={{
              fontFamily: "'JetBrains Mono'",
              fontSize: 13,
              color: "rgba(240,237,232,0.25)",
              letterSpacing: 2,
              margin: 0,
            }}
          >
            BUILT FOR FOUNDERS WHO&apos;D RATHER SHIP CODE THAN WRITE AD COPY
          </p>
        </section>

        {/* Agent hierarchy section */}
        <section ref={agentRef} style={{ maxWidth: 1200, margin: "0 auto", padding: "120px 48px" }}>
          <div style={{ textAlign: "center" as const, marginBottom: 64 }}>
            <div
              style={{
                fontFamily: "'JetBrains Mono'",
                fontSize: 12,
                letterSpacing: 3,
                color: "rgba(240,237,232,0.3)",
                textTransform: "uppercase" as const,
                marginBottom: 16,
              }}
            >
              YOUR MARKETING ORG
            </div>
            <h2
              style={{
                fontFamily: "'Instrument Serif'",
                fontSize: "clamp(36px, 4vw, 52px)",
                fontWeight: 400,
                lineHeight: 1.15,
                margin: 0,
              }}
            >
              Five agents. <span style={{ fontStyle: "italic", color: "#E94560" }}>One team.</span>
            </h2>
          </div>

          {/* Org chart */}
          <div style={{ display: "flex", flexDirection: "column" as const, alignItems: "center", gap: 24 }}>
            <AgentNode name="ARIA CEO" role="STRATEGIST" color="#E94560" delay={0} active={agentsVisible} />
            <div
              style={{
                width: 1,
                height: 32,
                background: "rgba(255,255,255,0.08)",
                opacity: agentsVisible ? 1 : 0,
                transition: "opacity 0.5s 0.3s",
              }}
            />
            <div style={{ display: "flex", gap: 16, flexWrap: "wrap" as const, justifyContent: "center" }}>
              <AgentNode name="ContentWriter" role="CONTENT" color="#4ECDC4" delay={400} active={agentsVisible} />
              <AgentNode name="EmailMarketer" role="EMAIL" color="#FFE66D" delay={550} active={agentsVisible} />
              <AgentNode name="SocialManager" role="SOCIAL" color="#A78BFA" delay={700} active={agentsVisible} />
              <AgentNode name="AdStrategist" role="ADS" color="#FF8A5C" delay={850} active={agentsVisible} />
            </div>
          </div>
        </section>

        {/* Features */}
        <section id="features" ref={featRef} style={{ maxWidth: 1200, margin: "0 auto", padding: "40px 48px 120px" }}>
          <div style={{ textAlign: "center" as const, marginBottom: 64 }}>
            <div
              style={{
                fontFamily: "'JetBrains Mono'",
                fontSize: 12,
                letterSpacing: 3,
                color: "rgba(240,237,232,0.3)",
                textTransform: "uppercase" as const,
                marginBottom: 16,
              }}
            >
              FEATURES
            </div>
            <h2
              style={{
                fontFamily: "'Instrument Serif'",
                fontSize: "clamp(36px, 4vw, 52px)",
                fontWeight: 400,
                lineHeight: 1.15,
                margin: 0,
              }}
            >
              Strategy <span style={{ fontStyle: "italic" }}>and</span> execution.
              <br />
              Not one or the other.
            </h2>
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
              gap: 20,
            }}
          >
            {features.map((f, i) => (
              <FeatureCard key={i} icon={f.icon} title={f.title} description={f.description} index={i} inView={featInView} />
            ))}
          </div>
        </section>

        {/* How it works */}
        <section
          id="how"
          ref={howRef}
          style={{
            maxWidth: 1200,
            margin: "0 auto",
            padding: "120px 48px",
            borderTop: "1px solid rgba(255,255,255,0.04)",
          }}
        >
          <div style={{ display: "flex", gap: 80, flexWrap: "wrap" as const }}>
            <div style={{ flex: "1 1 300px" }}>
              <div
                style={{
                  fontFamily: "'JetBrains Mono'",
                  fontSize: 12,
                  letterSpacing: 3,
                  color: "rgba(240,237,232,0.3)",
                  textTransform: "uppercase" as const,
                  marginBottom: 16,
                }}
              >
                HOW IT WORKS
              </div>
              <h2
                style={{
                  fontFamily: "'Instrument Serif'",
                  fontSize: "clamp(36px, 4vw, 48px)",
                  fontWeight: 400,
                  lineHeight: 1.15,
                  margin: "0 0 24px",
                }}
              >
                From zero to
                <br />
                <span style={{ fontStyle: "italic", color: "#E94560" }}>full marketing pipeline</span>
              </h2>
              <p
                style={{
                  fontFamily: "'Sora'",
                  fontSize: 16,
                  color: "rgba(240,237,232,0.4)",
                  lineHeight: 1.7,
                  maxWidth: 400,
                }}
              >
                Tell ARIA about your product once. Get a strategy and ready-to-use marketing assets &mdash; blog posts, emails, social posts, ad campaigns &mdash; on an ongoing basis.
              </p>
            </div>
            <div style={{ flex: "1 1 400px", display: "flex", flexDirection: "column" as const, gap: 40 }}>
              <Step number="1" title="Tell ARIA about your product" desc="A 10-minute conversation. Your product, your audience, your goals, your budget. ARIA builds a complete GTM playbook from this." index={0} inView={howInView} />
              <Step number="2" title="Review your GTM strategy" desc="A 30/60/90-day plan with specific channels, content themes, and campaign ideas. Tailored to your product and audience \u2014 not generic advice." index={1} inView={howInView} />
              <Step number="3" title="Your agents go to work" desc="Content, emails, social posts, and ad campaigns materialize on your calendar. Each piece ties back to your strategy and builds on the last." index={2} inView={howInView} />
              <Step number="4" title="Copy, paste, grow" desc="Everything is ready to use. Paste blog posts into your CMS, emails into your ESP, ad copy into Meta Ads Manager. Follow the step-by-step instructions." index={3} inView={howInView} />
            </div>
          </div>
        </section>

        {/* Facebook Ads callout */}
        <section style={{ maxWidth: 1200, margin: "0 auto", padding: "80px 48px" }}>
          <div
            style={{
              background: "rgba(233,69,96,0.04)",
              border: "1px solid rgba(233,69,96,0.12)",
              borderRadius: 20,
              padding: "60px 48px",
              display: "flex",
              gap: 48,
              flexWrap: "wrap" as const,
              alignItems: "center",
            }}
          >
            <div style={{ flex: "1 1 400px" }}>
              <div
                style={{
                  fontFamily: "'JetBrains Mono'",
                  fontSize: 12,
                  letterSpacing: 3,
                  color: "#E94560",
                  textTransform: "uppercase" as const,
                  marginBottom: 16,
                }}
              >
                FACEBOOK ADS &mdash; ZERO EXPERIENCE REQUIRED
              </div>
              <h3
                style={{
                  fontFamily: "'Instrument Serif'",
                  fontSize: 36,
                  fontWeight: 400,
                  lineHeight: 1.2,
                  margin: "0 0 20px",
                }}
              >
                ARIA writes your ads <span style={{ fontStyle: "italic" }}>and</span> teaches you
                <br />
                how to run them.
              </h3>
              <p
                style={{
                  fontFamily: "'Sora'",
                  fontSize: 15,
                  color: "rgba(240,237,232,0.5)",
                  lineHeight: 1.7,
                }}
              >
                Never opened Meta Ads Manager? No problem. ARIA&apos;s Ad Strategist produces complete campaign plans with ad copy, targeting parameters, budget recommendations, and numbered step-by-step instructions for every click in the interface.
              </p>
            </div>
            <div
              style={{
                flex: "1 1 300px",
                background: "rgba(0,0,0,0.3)",
                borderRadius: 12,
                padding: "24px",
                fontFamily: "'JetBrains Mono'",
                fontSize: 13,
                lineHeight: 1.8,
                color: "rgba(240,237,232,0.6)",
                border: "1px solid rgba(255,255,255,0.06)",
              }}
            >
              <div style={{ color: "#FF8A5C", marginBottom: 8 }}>AdStrategist &rarr;</div>
              <div style={{ color: "rgba(240,237,232,0.45)" }}>
                Campaign: &quot;DevTool Signups&quot;
                <br />
                Objective: Conversions
                <br />
                Daily budget: $25
                <br />
                Audience: Software devs, 25-44
                <br />
                <br />
                <span style={{ color: "#E94560" }}>Ad Copy (Variant A):</span>
                <br />
                &quot;Still deploying on Fridays
                <br />
                and praying? There&apos;s a better
                <br />
                way. Try [Product] free &rarr;&quot;
                <br />
                <br />
                <span style={{ color: "rgba(240,237,232,0.3)" }}>+ 3 more variants...</span>
              </div>
            </div>
          </div>
        </section>

        {/* Pricing */}
        <section
          id="pricing"
          ref={priceRef}
          style={{
            maxWidth: 1200,
            margin: "0 auto",
            padding: "120px 48px",
            borderTop: "1px solid rgba(255,255,255,0.04)",
          }}
        >
          <div style={{ textAlign: "center" as const, marginBottom: 64 }}>
            <div
              style={{
                fontFamily: "'JetBrains Mono'",
                fontSize: 12,
                letterSpacing: 3,
                color: "rgba(240,237,232,0.3)",
                textTransform: "uppercase" as const,
                marginBottom: 16,
              }}
            >
              PRICING
            </div>
            <h2
              style={{
                fontFamily: "'Instrument Serif'",
                fontSize: "clamp(36px, 4vw, 52px)",
                fontWeight: 400,
                lineHeight: 1.15,
                margin: "0 0 16px",
              }}
            >
              Less than a freelancer.
              <br />
              <span style={{ fontStyle: "italic", color: "#E94560" }}>Better than an agency.</span>
            </h2>
          </div>
          <div style={{ display: "flex", gap: 20, flexWrap: "wrap" as const }}>
            <PricingCard
              tier="Starter"
              price="$49"
              highlighted={false}
              index={0}
              inView={priceInView}
              features={["GTM playbook generation", "10 content pieces / month", "Content calendar", "1 campaign plan / month"]}
            />
            <PricingCard
              tier="Growth"
              price="$149"
              highlighted={true}
              index={1}
              inView={priceInView}
              features={["Everything in Starter", "30 content pieces / month", "Email sequences", "Social media calendar", "3 campaign plans / month", "Performance optimization"]}
            />
            <PricingCard
              tier="Scale"
              price="$299"
              highlighted={false}
              index={2}
              inView={priceInView}
              features={["Everything in Growth", "Unlimited content", "Priority generation", "Custom agent configs", "Dedicated support"]}
            />
          </div>
        </section>

        {/* Final CTA */}
        <section ref={ctaRef} style={{ maxWidth: 1200, margin: "0 auto", padding: "120px 48px 160px", textAlign: "center" as const }}>
          <h2
            style={{
              fontFamily: "'Instrument Serif'",
              fontSize: "clamp(40px, 5vw, 64px)",
              fontWeight: 400,
              lineHeight: 1.15,
              margin: "0 0 24px",
              opacity: ctaInView ? 1 : 0,
              transform: ctaInView ? "translateY(0)" : "translateY(20px)",
              transition: "all 0.8s cubic-bezier(0.16, 1, 0.3, 1)",
            }}
          >
            Stop Googling
            <br />
            &quot;<span style={{ fontStyle: "italic", color: "#E94560" }}>how to market my startup</span>&quot;
          </h2>
          <p
            style={{
              fontFamily: "'Sora'",
              fontSize: 18,
              color: "rgba(240,237,232,0.45)",
              lineHeight: 1.7,
              maxWidth: 500,
              margin: "0 auto 40px",
              fontWeight: 300,
              opacity: ctaInView ? 1 : 0,
              transition: "opacity 0.8s 0.3s",
            }}
          >
            Join the waitlist. Be the first to get an AI marketing team that actually understands your product.
          </p>
          <div
            style={{
              display: "flex",
              gap: 12,
              justifyContent: "center",
              maxWidth: 480,
              margin: "0 auto",
              opacity: ctaInView ? 1 : 0,
              transition: "opacity 0.8s 0.5s",
            }}
          >
            <input
              type="email"
              placeholder="you@startup.com"
              style={{
                flex: 1,
                padding: "16px 20px",
                background: "rgba(255,255,255,0.04)",
                border: "1px solid rgba(255,255,255,0.1)",
                borderRadius: 10,
                fontFamily: "'Sora'",
                fontSize: 15,
                color: "#F0EDE8",
                outline: "none",
              }}
              onFocus={(e) => ((e.target as HTMLInputElement).style.borderColor = "rgba(233,69,96,0.4)")}
              onBlur={(e) => ((e.target as HTMLInputElement).style.borderColor = "rgba(255,255,255,0.1)")}
            />
            <button
              style={{
                fontFamily: "'Sora'",
                fontSize: 15,
                fontWeight: 500,
                background: "#E94560",
                border: "none",
                borderRadius: 10,
                color: "#fff",
                padding: "16px 32px",
                cursor: "pointer",
                transition: "all 0.3s ease",
                whiteSpace: "nowrap" as const,
              }}
              onMouseEnter={(e) => {
                (e.target as HTMLButtonElement).style.boxShadow = "0 8px 32px rgba(233,69,96,0.4)";
              }}
              onMouseLeave={(e) => {
                (e.target as HTMLButtonElement).style.boxShadow = "";
              }}
            >
              Join Waitlist
            </button>
          </div>
        </section>

        {/* Footer */}
        <footer
          style={{
            borderTop: "1px solid rgba(255,255,255,0.04)",
            padding: "40px 48px",
            textAlign: "center" as const,
          }}
        >
          <p
            style={{
              fontFamily: "'JetBrains Mono'",
              fontSize: 12,
              color: "rgba(240,237,232,0.2)",
              margin: 0,
            }}
          >
            &copy; 2026 ARIA. Built by founders, for founders.
          </p>
        </footer>
      </div>
    </div>
  );
}
