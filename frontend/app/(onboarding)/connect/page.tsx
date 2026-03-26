"use client";

import React, { useState } from "react";

interface Integration {
  id: string;
  name: string;
  description: string;
  color: string;
  letter: string;
  connected: boolean;
}

const initialIntegrations: Integration[] = [
  { id: "gmail", name: "Gmail", description: "Send emails, manage follow-ups, and sync conversations", color: "#EA4335", letter: "G", connected: false },
  { id: "whatsapp", name: "WhatsApp", description: "Automate customer messaging and support chats", color: "#25D366", letter: "W", connected: false },
  { id: "facebook", name: "Facebook", description: "Manage leads from Facebook Ads and Messenger", color: "#1877F2", letter: "f", connected: false },
  { id: "instagram", name: "Instagram", description: "Respond to DMs and manage comments automatically", color: "#E4405F", letter: "I", connected: false },
  { id: "shopify", name: "Shopify", description: "Sync orders, inventory, and customer data", color: "#96BF48", letter: "S", connected: false },
  { id: "quickbooks", name: "QuickBooks", description: "Automate invoicing and payment tracking", color: "#2CA01C", letter: "Q", connected: false },
  { id: "hubspot", name: "HubSpot", description: "Sync contacts, deals, and pipeline data", color: "#FF7A59", letter: "H", connected: false },
  { id: "calendly", name: "Calendly", description: "Automate appointment booking and reminders", color: "#006BFF", letter: "C", connected: false },
  { id: "slack", name: "Slack", description: "Get agent notifications and team alerts in Slack", color: "#4A154B", letter: "S", connected: false },
];

export default function ConnectPage() {
  const [integrations, setIntegrations] = useState(initialIntegrations);

  function handleConnect(id: string) {
    setIntegrations((prev) =>
      prev.map((i) => (i.id === id ? { ...i, connected: true } : i))
    );
  }

  const connectedCount = integrations.filter((i) => i.connected).length;

  return (
    <div className="max-w-4xl mx-auto px-6 py-10">
      {/* Header */}
      <div className="text-center mb-10">
        <h1 className="text-[28px] font-bold text-[#2C2C2A] mb-2">Connect your existing tools</h1>
        <p className="text-[#5F5E5A] text-[15px] max-w-lg mx-auto">
          Link the tools you already use so ARIA agents can work with your data. You can always add more later.
        </p>
        {connectedCount > 0 && (
          <div className="mt-3 inline-flex items-center gap-1.5 bg-[#E6F7F0] text-[#1D9E75] text-sm font-medium px-3 py-1 rounded-full">
            <svg width="14" height="14" fill="none" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            {connectedCount} tool{connectedCount > 1 ? "s" : ""} connected
          </div>
        )}
      </div>

      {/* Integration cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mb-10">
        {integrations.map((integration) => (
          <div
            key={integration.id}
            className={`bg-white rounded-xl border p-5 transition ${
              integration.connected ? "border-[#1D9E75] bg-[#FAFFF9]" : "border-[#E0DED8] hover:border-[#534AB7]/30"
            }`}
          >
            {/* Icon + status */}
            <div className="flex items-start justify-between mb-3">
              <div
                className="w-10 h-10 rounded-lg flex items-center justify-center text-white font-bold text-lg"
                style={{ backgroundColor: integration.color }}
              >
                {integration.letter}
              </div>
              {integration.connected && (
                <span className="inline-flex items-center gap-1 bg-[#E6F7F0] text-[#1D9E75] text-xs font-semibold px-2 py-0.5 rounded-full">
                  <svg width="12" height="12" fill="none" viewBox="0 0 24 24"><path d="M20 6L9 17l-5-5" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
                  Connected
                </span>
              )}
            </div>

            <h3 className="text-sm font-semibold text-[#2C2C2A] mb-1">{integration.name}</h3>
            <p className="text-xs text-[#5F5E5A] leading-relaxed mb-4">{integration.description}</p>

            {/* Actions */}
            {integration.connected ? (
              <button
                className="w-full h-9 rounded-lg border border-[#E0DED8] text-xs text-[#5F5E5A] font-medium hover:bg-[#F8F8F6] transition"
              >
                Disconnect
              </button>
            ) : (
              <div className="flex gap-2">
                <button
                  onClick={() => handleConnect(integration.id)}
                  className="flex-1 h-9 rounded-lg bg-[#534AB7] text-white text-xs font-semibold hover:bg-[#4840A0] transition"
                >
                  Connect
                </button>
                <button className="h-9 px-3 rounded-lg border border-[#E0DED8] text-xs text-[#5F5E5A] font-medium hover:bg-[#F8F8F6] transition">
                  Skip
                </button>
              </div>
            )}
          </div>
        ))}
      </div>

      {/* CTA */}
      <div className="flex items-center justify-between">
        <a href="/select-agents" className="text-sm text-[#5F5E5A] hover:text-[#2C2C2A] font-medium flex items-center gap-1.5">
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/></svg>
          Back
        </a>
        <a
          href="/launching"
          className="inline-flex items-center gap-2 h-11 px-8 rounded-lg bg-[#534AB7] text-white font-semibold text-sm hover:bg-[#4840A0] transition shadow-sm"
        >
          Start ARIA
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
        </a>
      </div>
    </div>
  );
}
