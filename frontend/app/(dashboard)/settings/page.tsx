"use client";

import React, { useState, useEffect, useRef } from "react";
import { supabase } from "@/lib/supabase";
import { API_URL, authFetch, email as emailApi, EmailSettingsStatus } from "@/lib/api";
import { useConfirm } from "@/lib/use-confirm";
import { useNotifications } from "@/lib/use-notifications";

const settingsTabs = ["Profile", "Integrations", "Email", "Notifications", "Billing"];

// "Coming soon" list — only shows integrations that are NOT already
// live in the Active Integrations panel above. X / Twitter and
// LinkedIn previously appeared in both (listed as Active with a
// Connect button AND duplicated as "Coming in v2") — confusing, so
// they were removed from this list once the OAuth connect flows
// shipped. Add anything still genuinely not live here.
const futureIntegrations = [
  { name: "Mailchimp", description: "Email marketing automation", phase: "v1.5" },
  { name: "ConvertKit", description: "Creator email marketing", phase: "v1.5" },
  { name: "Meta Ads", description: "Automated ad campaign management", phase: "v2.5" },
  { name: "Google Analytics", description: "Traffic and conversion analytics", phase: "v3" },
];

export default function SettingsPage() {
  // Track all OAuth message listeners + safety timers so we can clean
  // them up if the component unmounts before the popup completes its
  // postMessage. Without this, listeners leaked: a popup closed cleanly
  // by the user (no postMessage) would leave a window listener active
  // until its 15-30s safety timer fired -- and if the user navigated
  // away first, the listener stayed pinned to the SettingsPage closure
  // forever.
  const oauthCleanupRef = useRef<Array<() => void>>([]);
  useEffect(() => {
    return () => {
      // Run all cleanup callbacks on unmount
      for (const cleanup of oauthCleanupRef.current) {
        try { cleanup(); } catch {}
      }
      oauthCleanupRef.current = [];
    };
  }, []);

  const confirmModal = useConfirm();
  const { showToast } = useNotifications();
  const [activeTab, setActiveTab] = useState("Profile");
  const [user, setUser] = useState({ name: "", email: "", company: "" });

  // Email tab state — display name is editable; provider/domain/
  // addresses come from the backend status endpoint. Once Coder 2
  // ships PATCH /api/settings/{tenant_id}/email, the save flow lights
  // up automatically; until then we degrade to a stubbed save that
  // still shows the user a toast.
  const [emailStatus, setEmailStatus] = useState<EmailSettingsStatus | null>(null);
  const [emailStatusLoaded, setEmailStatusLoaded] = useState(false);
  const [emailDisplayName, setEmailDisplayName] = useState("");
  const [emailSaving, setEmailSaving] = useState(false);
  const [emailGmailAdvancedOpen, setEmailGmailAdvancedOpen] = useState(false);
  const [emailReplyToCopied, setEmailReplyToCopied] = useState(false);

  const handleSignOut = async () => {
    const ok = await confirmModal.confirm({
      title: "Sign out of ARIA?",
      message: "You'll need to log in again to access your workspace.",
      confirmLabel: "Sign out",
      cancelLabel: "Stay signed in",
    });
    if (!ok) return;
    await supabase.auth.signOut();
    // Match sidebar sign-out behavior — land on the marketing page,
    // not the login form.
    window.location.href = "/";
  };
  const [gmailConnected, setGmailConnected] = useState<boolean | null>(null);
  const [gmailEmail, setGmailEmail] = useState<string>("");
  const [gmailReconnecting, setGmailReconnecting] = useState(false);
  const [twitterConnected, setTwitterConnected] = useState<boolean | null>(null);
  const [twitterUsername, setTwitterUsername] = useState("");
  const [whatsappConnected, setWhatsappConnected] = useState<boolean | null>(null);
  const [whatsappConnecting, setWhatsappConnecting] = useState(false);
  const [whatsappForm, setWhatsappForm] = useState({ access_token: "", phone_number_id: "", business_account_id: "" });
  const [whatsappShowForm, setWhatsappShowForm] = useState(false);
  const [whatsappError, setWhatsappError] = useState("");
  const [linkedinConnected, setLinkedinConnected] = useState<boolean | null>(null);
  const [linkedinName, setLinkedinName] = useState("");
  const [linkedinPostingTo, setLinkedinPostingTo] = useState<"personal" | "company">("personal");
  const [linkedinOrgName, setLinkedinOrgName] = useState("");
  const [linkedinOrgs, setLinkedinOrgs] = useState<{ id: string; name: string; urn: string }[]>([]);
  const [linkedinOrgsLoading, setLinkedinOrgsLoading] = useState(false);
  const [linkedinShowOrgs, setLinkedinShowOrgs] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (session?.user) {
        const meta = session.user.user_metadata;
        setUser({
          name: meta?.full_name || meta?.name || "",
          email: session.user.email || "",
          company: meta?.company || "",
        });
      }
    });
    // Check Gmail connection status
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (tenantId) {
      authFetch(`${API_URL}/api/integrations/${tenantId}/gmail-status`)
        .then(r => r.json())
        .then(data => { setGmailConnected(!!data?.connected); setGmailEmail(data?.email || ""); })
        .catch(() => setGmailConnected(false));
      authFetch(`${API_URL}/api/integrations/${tenantId}/twitter-status`)
        .then(r => r.json())
        .then(data => { setTwitterConnected(!!data?.connected); setTwitterUsername(data?.username || ""); })
        .catch(() => setTwitterConnected(false));
      authFetch(`${API_URL}/api/integrations/${tenantId}/whatsapp-status`)
        .then(r => r.json())
        .then(data => setWhatsappConnected(!!data?.connected))
        .catch(() => setWhatsappConnected(false));
      authFetch(`${API_URL}/api/integrations/${tenantId}/linkedin-status`)
        .then(r => r.json())
        .then(data => {
          setLinkedinConnected(!!data?.connected);
          setLinkedinName(data?.name || "");
          setLinkedinPostingTo(data?.posting_to || "personal");
          setLinkedinOrgName(data?.org_name || "");
        })
        .catch(() => setLinkedinConnected(false));

      // Email sending config — best effort, the backend endpoint is
      // shipping in a parallel branch. If it 404s we surface a "not
      // configured yet" status panel instead of breaking the tab.
      emailApi.getStatus(tenantId)
        .then((data) => {
          setEmailStatus(data);
          setEmailDisplayName(data?.display_name || "");
        })
        .catch(() => setEmailStatus(null))
        .finally(() => setEmailStatusLoaded(true));
    }
  }, []);

  // Default the display-name field to the user's profile name once
  // both the user and the email status have settled, but only if the
  // backend hasn't already persisted one.
  useEffect(() => {
    if (emailStatusLoaded && !emailDisplayName && user.name) {
      setEmailDisplayName(user.name);
    }
  }, [emailStatusLoaded, user.name, emailDisplayName]);

  async function saveEmailConfig() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId) return;
    setEmailSaving(true);
    try {
      const updated = await emailApi.updateConfig(tenantId, {
        display_name: emailDisplayName.trim(),
      });
      setEmailStatus(updated);
      showToast({ title: "Email settings saved", variant: "success" });
    } catch {
      // Backend not ready yet — keep the UX optimistic so the user
      // doesn't think the save was lost.
      showToast({
        title: "Email config saved",
        body: "Provider integration in progress.",
        variant: "info",
      });
    } finally {
      setEmailSaving(false);
    }
  }

  async function copyToClipboard(value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setEmailReplyToCopied(true);
      setTimeout(() => setEmailReplyToCopied(false), 1500);
    } catch {
      // Older browsers / insecure context — quietly no-op.
    }
  }

  function reconnectGmail() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId) return;
    setGmailReconnecting(true);

    function refreshGmailStatus() {
      authFetch(`${API_URL}/api/integrations/${tenantId}/gmail-status`)
        .then(r => r.json())
        .then(data => { setGmailConnected(!!data?.connected); setGmailEmail(data?.email || ""); })
        .catch(() => {});
      setGmailReconnecting(false);
    }

    // Listen for postMessage from the popup. Cleanup is registered in
    // oauthCleanupRef so an unmount during the popup window can still
    // remove the listener instead of leaking it for 15s.
    let cleaned = false;
    function cleanup() {
      if (cleaned) return;
      cleaned = true;
      window.removeEventListener("message", onMessage);
      clearInterval(timer);
      clearTimeout(safetyTimer);
    }
    function onMessage(e: MessageEvent) {
      if (e.data === "gmail_connected") {
        cleanup();
        refreshGmailStatus();
      }
    }
    window.addEventListener("message", onMessage);

    // Do NOT pin login_hint to the user's ARIA email. If the user signed
    // into ARIA with a Google Workspace for Education / Workspace for
    // Business account, that account's admin almost always has third-
    // party OAuth with sensitive Gmail scopes blocked — and forcing it
    // via login_hint just deterministically reproduces the
    // "Access blocked: Authorization Error / Error 400: invalid_request"
    // page even when the user has a perfectly working personal Gmail
    // signed in to the same browser. Letting Google show its native
    // account picker (via prompt=select_account on the backend) lets
    // the user pick whichever Google account actually has Gmail OAuth
    // enabled, regardless of which account they used to sign into ARIA.
    const popup = window.open(
      `${API_URL}/api/auth/google/connect/${tenantId}`,
      "google_auth",
      "width=600,height=700"
    );
    // Fallback: poll for popup close (may fail cross-origin, so also use timer)
    const timer = setInterval(() => {
      try { if (!popup || popup.closed) { cleanup(); refreshGmailStatus(); } } catch { /* cross-origin */ }
    }, 500);
    // Safety fallback: refresh status after 15s regardless
    const safetyTimer = setTimeout(() => { cleanup(); refreshGmailStatus(); }, 15000);
    oauthCleanupRef.current.push(cleanup);
  }

  function connectTwitter() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId) return;
    window.open(`${API_URL}/api/auth/twitter/connect/${tenantId}`, "twitter_auth", "width=600,height=700");
    let cleaned = false;
    function cleanup() {
      if (cleaned) return;
      cleaned = true;
      window.removeEventListener("message", onMsg);
      clearTimeout(safetyTimer);
    }
    function onMsg(e: MessageEvent) {
      if (e.data === "twitter_connected") {
        cleanup();
        authFetch(`${API_URL}/api/integrations/${tenantId}/twitter-status`).then(r => r.json()).then(data => { setTwitterConnected(!!data?.connected); setTwitterUsername(data?.username || ""); }).catch(() => {});
      }
    }
    window.addEventListener("message", onMsg);
    const safetyTimer = setTimeout(cleanup, 30000);
    oauthCleanupRef.current.push(cleanup);
  }

  function connectLinkedIn() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId) return;
    window.open(`${API_URL}/api/auth/linkedin/connect/${tenantId}`, "linkedin_auth", "width=600,height=700");
    let cleaned = false;
    function cleanup() {
      if (cleaned) return;
      cleaned = true;
      window.removeEventListener("message", onMsg);
      clearTimeout(safetyTimer);
    }
    function onMsg(e: MessageEvent) {
      if (e.data === "linkedin_connected") {
        cleanup();
        authFetch(`${API_URL}/api/integrations/${tenantId}/linkedin-status`).then(r => r.json()).then(data => { setLinkedinConnected(!!data?.connected); setLinkedinName(data?.name || ""); setLinkedinPostingTo(data?.posting_to || "personal"); setLinkedinOrgName(data?.org_name || ""); }).catch(() => {});
      }
    }
    window.addEventListener("message", onMsg);
    const safetyTimer = setTimeout(cleanup, 30000);
    oauthCleanupRef.current.push(cleanup);
  }

  async function fetchLinkedInOrgs() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId) return;
    setLinkedinOrgsLoading(true);
    try {
      const res = await authFetch(`${API_URL}/api/linkedin/${tenantId}/organizations`);
      const data = await res.json();
      setLinkedinOrgs(data.organizations || []);
      setLinkedinShowOrgs(true);
    } catch {
      alert("Failed to fetch company pages");
    } finally {
      setLinkedinOrgsLoading(false);
    }
  }

  async function setLinkedinTarget(orgUrn: string, orgName: string) {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId) return;
    try {
      await authFetch(`${API_URL}/api/linkedin/${tenantId}/set-target`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ org_urn: orgUrn, org_name: orgName }),
      });
      setLinkedinPostingTo(orgUrn ? "company" : "personal");
      setLinkedinOrgName(orgName);
      setLinkedinShowOrgs(false);
    } catch {
      alert("Failed to update posting target");
    }
  }

  async function connectWhatsApp() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId) return;
    setWhatsappConnecting(true);
    setWhatsappError("");
    try {
      const res = await authFetch(`${API_URL}/api/whatsapp/${tenantId}/connect`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(whatsappForm),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.detail || `Failed (${res.status})`);
      }
      setWhatsappConnected(true);
      setWhatsappShowForm(false);
      setWhatsappForm({ access_token: "", phone_number_id: "", business_account_id: "" });
    } catch (e: any) {
      setWhatsappError(e.message || "Connection failed");
    } finally {
      setWhatsappConnecting(false);
    }
  }

  async function disconnectGmail() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId || !confirm("Disconnect Gmail? Email Marketer will only be able to draft, not send.")) return;
    await authFetch(`${API_URL}/api/integrations/${tenantId}/gmail-disconnect`, { method: "POST" });
    setGmailConnected(false);
  }

  async function disconnectTwitter() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId || !confirm("Disconnect X/Twitter? Social Manager will only be able to draft, not publish.")) return;
    await authFetch(`${API_URL}/api/integrations/${tenantId}/twitter-disconnect`, { method: "POST" });
    setTwitterConnected(false);
    setTwitterUsername("");
  }

  async function disconnectLinkedIn() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId || !confirm("Disconnect LinkedIn? You won't be able to publish posts to LinkedIn.")) return;
    await authFetch(`${API_URL}/api/integrations/${tenantId}/linkedin-disconnect`, { method: "POST" });
    setLinkedinConnected(false);
    setLinkedinName("");
    setLinkedinPostingTo("personal");
    setLinkedinOrgName("");
  }

  async function disconnectWhatsApp() {
    const tenantId = localStorage.getItem("aria_tenant_id");
    if (!tenantId || !confirm("Disconnect WhatsApp?")) return;
    await authFetch(`${API_URL}/api/whatsapp/${tenantId}/disconnect`, { method: "POST" });
    setWhatsappConnected(false);
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">Settings</h1>

      {/* Tabs */}
      <div className="flex items-center gap-1 border-b border-[#E0DED8]">
        {settingsTabs.map((tab) => (
          <button key={tab} onClick={() => setActiveTab(tab)} className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px ${activeTab === tab ? "border-[#534AB7] text-[#534AB7]" : "border-transparent text-[#5F5E5A] hover:text-[#2C2C2A]"}`}>
            {tab}
          </button>
        ))}
      </div>

      {/* Profile */}
      {activeTab === "Profile" && (
        <div className="space-y-6">
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-4">Your Profile</h2>
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Full Name</label>
                <input type="text" value={user.name} onChange={(e) => setUser({ ...user, name: e.target.value })} className="w-full px-3 py-2.5 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7]" />
              </div>
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Email</label>
                <input type="email" value={user.email} disabled className="w-full px-3 py-2.5 bg-[#F8F8F6] border border-[#E0DED8] rounded-lg text-sm text-[#5F5E5A] cursor-not-allowed" />
              </div>
              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Company / Product</label>
                <input type="text" value={user.company} onChange={(e) => setUser({ ...user, company: e.target.value })} className="w-full px-3 py-2.5 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7]" />
              </div>
            </div>
            <button className="mt-4 px-4 py-2 bg-[#534AB7] text-white rounded-lg text-sm font-medium hover:bg-[#433AA0] transition-colors">Save changes</button>
          </div>

          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-2">Business Profile</h2>
            <p className="text-sm text-[#5F5E5A] mb-4">Edit or redo your onboarding to update how ARIA markets your product.</p>
            <div className="flex items-center gap-3">
              <a href="/edit-profile" className="px-4 py-2 bg-[#534AB7] text-white rounded-lg text-sm font-medium hover:bg-[#433AA0] transition-colors">
                Edit answers
              </a>
              <a href="/welcome" className="px-4 py-2 border border-[#E0DED8] text-[#5F5E5A] rounded-lg text-sm font-medium hover:bg-[#F8F8F6] transition-colors">
                Restart onboarding
              </a>
            </div>
          </div>

          {/* Brand Voice tone picker removed — it was non-functional
              placeholder UI (no onClick, no persistence). Brand voice
              is captured during onboarding as free-text in
              gtm_playbook.brand_voice.tone and consumed by every
              sub-agent via BaseAgent.business_context(). To change
              it, use the Business Profile → Edit answers /
              Restart onboarding buttons above. A 4-preset tone
              picker would overwrite the user's nuanced onboarding
              answer with a single word, which is a regression. */}

          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A] mb-2">Account</h2>
            <div className="flex items-center justify-between py-3 border-b border-[#E0DED8]">
              <div>
                <p className="text-sm font-medium text-[#2C2C2A]">Change password</p>
                <p className="text-xs text-[#5F5E5A]">Update your account password</p>
              </div>
              <button className="text-xs font-medium px-4 py-2 border border-[#E0DED8] rounded-lg text-[#5F5E5A] hover:bg-[#F8F8F6]">Change</button>
            </div>
            <div className="flex items-center justify-between py-3">
              <div>
                <p className="text-sm font-medium text-[#D85A30]">Sign out</p>
                <p className="text-xs text-[#5F5E5A]">Sign out of your ARIA account</p>
              </div>
              <button onClick={handleSignOut} className="text-xs font-medium px-4 py-2 border border-[#D85A30] rounded-lg text-[#D85A30] hover:bg-[#FDEEE8]">Sign out</button>
            </div>
          </div>
        </div>
      )}

      {/* Integrations */}
      {activeTab === "Integrations" && (
        <div className="space-y-4">
          {/* Gmail card has been moved to Settings → Email → Advanced
              disclosure. ARIA now sends transactional + campaign mail
              through its own managed provider (Resend); Gmail OAuth is
              opt-in for the small subset of users who still need to
              send from their personal Gmail address. */}

          {/* Twitter / X */}
          <div className="bg-white rounded-xl border border-[#E0DED8]">
            <div className="px-5 py-4 flex items-center justify-between gap-3">
              <div className="flex items-center gap-3 flex-1 min-w-0">
                <div className="w-9 h-9 rounded-lg bg-[#F0F0F0] flex items-center justify-center shrink-0">
                  <svg className="w-5 h-5 text-[#2C2C2A]" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" />
                  </svg>
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-[#2C2C2A]">X / Twitter</p>
                  <p className="text-xs text-[#5F5E5A] mt-0.5 break-words">
                    {twitterConnected === null ? "Checking..." : twitterConnected
                      ? `Connected — @${twitterUsername}`
                      : "Not connected — Social Manager can only draft, not publish"}
                  </p>
                </div>
              </div>
              {twitterConnected === false ? (
                <button
                  onClick={connectTwitter}
                  className="text-xs font-medium px-4 py-2 rounded-lg bg-[#2C2C2A] text-white hover:bg-[#1a1a19] transition-colors shrink-0 whitespace-nowrap"
                >
                  Connect X
                </button>
              ) : twitterConnected ? (
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[10px] font-medium px-2.5 py-1 rounded-full bg-[#E6F5ED] text-[#1D9E75]">Connected</span>
                  <button onClick={connectTwitter} className="text-[10px] text-[#534AB7] hover:underline transition-colors">Reconnect</button>
                  <button onClick={disconnectTwitter} className="text-[10px] text-[#5F5E5A] hover:text-[#D85A30] transition-colors">Disconnect</button>
                </div>
              ) : null}
            </div>
          </div>

          {/* LinkedIn */}
          <div className="bg-white rounded-xl border border-[#E0DED8]">
            <div className="px-5 py-4 flex items-center justify-between gap-3">
              <div className="flex items-center gap-3 flex-1 min-w-0">
                <div className="w-9 h-9 rounded-lg bg-[#EBF4FB] flex items-center justify-center shrink-0">
                  <svg className="w-5 h-5 text-[#0A66C2]" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 01-2.063-2.065 2.064 2.064 0 112.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z" />
                  </svg>
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-[#2C2C2A]">LinkedIn</p>
                  <p className="text-xs text-[#5F5E5A] mt-0.5 break-words">
                    {linkedinConnected === null ? "Checking..." : linkedinConnected
                      ? `Connected — ${linkedinName}${linkedinPostingTo === "company" ? ` · Posting to ${linkedinOrgName}` : " · Posting to personal profile"}`
                      : "Not connected — connect to publish posts on LinkedIn"}
                  </p>
                </div>
              </div>
              {linkedinConnected === false ? (
                <button
                  onClick={connectLinkedIn}
                  className="text-xs font-medium px-4 py-2 rounded-lg bg-[#0A66C2] text-white hover:bg-[#084d93] transition-colors shrink-0 whitespace-nowrap"
                >
                  Connect LinkedIn
                </button>
              ) : linkedinConnected ? (
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[10px] font-medium px-2.5 py-1 rounded-full bg-[#E6F5ED] text-[#1D9E75]">Connected</span>
                  <button onClick={connectLinkedIn} className="text-[10px] text-[#534AB7] hover:underline transition-colors">Reconnect</button>
                  <button onClick={disconnectLinkedIn} className="text-[10px] text-[#5F5E5A] hover:text-[#D85A30] transition-colors">Disconnect</button>
                </div>
              ) : null}
            </div>
            {linkedinConnected && (
              <div className="px-5 pb-4 border-t border-[#E0DED8] pt-3">
                <div className="flex items-center justify-between">
                  <p className="text-xs font-medium text-[#5F5E5A]">Post to:</p>
                  <button
                    onClick={fetchLinkedInOrgs}
                    disabled={linkedinOrgsLoading}
                    className="text-xs text-[#0A66C2] hover:underline transition-colors"
                  >
                    {linkedinOrgsLoading ? "Loading..." : "Change posting target"}
                  </button>
                </div>
                <p className="text-sm text-[#2C2C2A] mt-1">
                  {linkedinPostingTo === "company" ? `🏢 ${linkedinOrgName}` : `👤 Personal profile (${linkedinName})`}
                </p>
                {linkedinShowOrgs && (
                  <div className="mt-3 space-y-2">
                    <button
                      onClick={() => setLinkedinTarget("", "")}
                      className={`w-full text-left px-3 py-2 rounded-lg border text-sm transition-colors ${linkedinPostingTo === "personal" ? "border-[#0A66C2] bg-[#EBF4FB] text-[#0A66C2]" : "border-[#E0DED8] text-[#2C2C2A] hover:bg-[#F8F8F6]"}`}
                    >
                      👤 Personal profile ({linkedinName})
                    </button>
                    {linkedinOrgs.map((org) => (
                      <button
                        key={org.urn}
                        onClick={() => setLinkedinTarget(org.urn, org.name)}
                        className={`w-full text-left px-3 py-2 rounded-lg border text-sm transition-colors ${linkedinPostingTo === "company" && linkedinOrgName === org.name ? "border-[#0A66C2] bg-[#EBF4FB] text-[#0A66C2]" : "border-[#E0DED8] text-[#2C2C2A] hover:bg-[#F8F8F6]"}`}
                      >
                        🏢 {org.name || `Organization ${org.id}`}
                      </button>
                    ))}
                    {linkedinOrgs.length === 0 && (
                      <p className="text-xs text-[#9E9C95]">No company pages found. You must be an admin of a LinkedIn Company Page.</p>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>

          {/* WhatsApp */}
          <div className="bg-white rounded-xl border border-[#E0DED8]">
            <div className="px-5 py-4 flex items-center justify-between gap-3">
              <div className="flex items-center gap-3 flex-1 min-w-0">
                <div className="w-9 h-9 rounded-lg bg-[#E8F5E8] flex items-center justify-center shrink-0">
                  <svg className="w-5 h-5 text-[#25D366]" viewBox="0 0 24 24" fill="currentColor">
                    <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z" />
                  </svg>
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-[#2C2C2A]">WhatsApp</p>
                  <p className="text-xs text-[#5F5E5A] mt-0.5 break-words">
                    {whatsappConnected === null ? "Checking..." : whatsappConnected
                      ? "Connected — WhatsApp Business API"
                      : "Not connected — connect to send messages via WhatsApp"}
                  </p>
                </div>
              </div>
              {whatsappConnected === false ? (
                <button
                  onClick={() => setWhatsappShowForm(!whatsappShowForm)}
                  className="text-xs font-medium px-4 py-2 rounded-lg bg-[#25D366] text-white hover:bg-[#1da851] transition-colors shrink-0 whitespace-nowrap"
                >
                  Connect WhatsApp
                </button>
              ) : whatsappConnected ? (
                <div className="flex items-center gap-2 shrink-0">
                  <span className="text-[10px] font-medium px-2.5 py-1 rounded-full bg-[#E6F5ED] text-[#1D9E75]">Connected</span>
                  <button onClick={() => { setWhatsappShowForm(true); setWhatsappConnected(false); }} className="text-[10px] text-[#534AB7] hover:underline transition-colors">Reconnect</button>
                  <button onClick={disconnectWhatsApp} className="text-[10px] text-[#5F5E5A] hover:text-[#D85A30] transition-colors">Disconnect</button>
                </div>
              ) : null}
            </div>
            {whatsappShowForm && (
              <div className="px-5 pb-4 border-t border-[#E0DED8] pt-4 space-y-3">
                <p className="text-xs text-[#5F5E5A]">Enter your WhatsApp Cloud API credentials from the <a href="https://developers.facebook.com" target="_blank" rel="noopener noreferrer" className="text-[#534AB7] hover:underline">Meta Developer Dashboard</a>.</p>
                <div>
                  <label className="block text-xs font-medium text-[#5F5E5A] mb-1">Phone Number ID</label>
                  <input type="text" value={whatsappForm.phone_number_id} onChange={e => setWhatsappForm({ ...whatsappForm, phone_number_id: e.target.value })} placeholder="e.g. 123456789012345" className="w-full px-3 py-2 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#25D366]/20 focus:border-[#25D366]" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-[#5F5E5A] mb-1">Business Account ID</label>
                  <input type="text" value={whatsappForm.business_account_id} onChange={e => setWhatsappForm({ ...whatsappForm, business_account_id: e.target.value })} placeholder="e.g. 987654321098765" className="w-full px-3 py-2 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#25D366]/20 focus:border-[#25D366]" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-[#5F5E5A] mb-1">Access Token</label>
                  <input type="password" value={whatsappForm.access_token} onChange={e => setWhatsappForm({ ...whatsappForm, access_token: e.target.value })} placeholder="Permanent token from System Users" className="w-full px-3 py-2 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] focus:outline-none focus:ring-2 focus:ring-[#25D366]/20 focus:border-[#25D366]" />
                </div>
                {whatsappError && <p className="text-xs text-[#D85A30]">{whatsappError}</p>}
                <button onClick={connectWhatsApp} disabled={whatsappConnecting || !whatsappForm.access_token || !whatsappForm.phone_number_id} className="px-4 py-2 bg-[#25D366] text-white rounded-lg text-sm font-medium hover:bg-[#1da851] transition-colors disabled:opacity-50">
                  {whatsappConnecting ? "Connecting..." : "Save & Verify"}
                </button>
              </div>
            )}
          </div>

          {/* Future integrations */}
          <div className="bg-white rounded-xl border border-[#E0DED8]">
            <div className="px-5 py-4 border-b border-[#E0DED8]">
              <h2 className="text-base font-semibold text-[#2C2C2A]">Coming Soon</h2>
              <p className="text-xs text-[#5F5E5A] mt-1">Direct integrations are coming in future versions.</p>
            </div>
            <div className="divide-y divide-[#E0DED8]">
              {futureIntegrations.map((int) => (
                <div key={int.name} className="px-5 py-4 flex items-center justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-[#2C2C2A]">{int.name}</p>
                    <p className="text-xs text-[#5F5E5A] mt-0.5 break-words">{int.description}</p>
                  </div>
                  <span className="text-[10px] font-medium px-2 py-1 rounded-full bg-[#F8F8F6] text-[#5F5E5A] shrink-0 whitespace-nowrap">Coming in {int.phase}</span>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Email — sender identity + Resend-managed sending config.
          The Connect Gmail card lives at the bottom of this tab, in
          an "Advanced" disclosure, since it's now opt-in rather than
          the primary path. */}
      {activeTab === "Email" && (
        <div className="space-y-4">
          {/* Header card */}
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h2 className="text-base font-semibold text-[#2C2C2A]">Email Sending Configuration</h2>
            <p className="text-sm text-[#5F5E5A] mt-1">
              ARIA sends emails on your behalf. Configure how recipients see your sender identity.
            </p>

            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mt-5">
              <div className="sm:col-span-2">
                <label htmlFor="email_display_name" className="block text-xs font-medium text-[#5F5E5A] mb-1.5">
                  Sender display name
                </label>
                <input
                  id="email_display_name"
                  type="text"
                  value={emailDisplayName}
                  onChange={(e) => setEmailDisplayName(e.target.value)}
                  placeholder={user.name || "Your full name"}
                  className="w-full h-11 px-3 bg-white border border-[#E0DED8] rounded-lg text-sm text-[#2C2C2A] placeholder:text-[#B0AFA8] focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 focus:border-[#534AB7]"
                />
                <p className="text-[11px] text-[#8A8983] mt-1">
                  Shown in the &quot;From&quot; header recipients see in their inbox.
                </p>
              </div>

              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Sender email</label>
                <div className="flex items-center h-11 px-3 bg-[#F8F8F6] border border-[#E0DED8] rounded-lg text-sm text-[#5F5E5A]">
                  <span className="truncate">{emailStatus?.sender_address || "Pending domain verification"}</span>
                </div>
                <p className="text-[11px] text-[#8A8983] mt-1">Auto-generated. The from-address recipients see.</p>
              </div>

              <div>
                <label className="block text-xs font-medium text-[#5F5E5A] mb-1.5">Reply-to address</label>
                <div className="flex items-center gap-2 h-11 px-3 bg-[#F8F8F6] border border-[#E0DED8] rounded-lg text-sm text-[#5F5E5A]">
                  <span className="truncate flex-1">{emailStatus?.reply_to_address || "Pending domain verification"}</span>
                  {emailStatus?.reply_to_address && (
                    <button
                      type="button"
                      onClick={() => copyToClipboard(emailStatus.reply_to_address!)}
                      className="text-[11px] font-medium text-[#534AB7] hover:underline shrink-0"
                    >
                      {emailReplyToCopied ? "Copied" : "Copy"}
                    </button>
                  )}
                </div>
                <p className="text-[11px] text-[#8A8983] mt-1">Replies route back into ARIA&apos;s inbound inbox.</p>
              </div>
            </div>

            <div className="mt-5 flex items-center gap-3">
              <button
                onClick={saveEmailConfig}
                disabled={emailSaving}
                className="px-4 py-2 bg-[#534AB7] text-white rounded-lg text-sm font-medium hover:bg-[#433AA0] transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {emailSaving ? "Saving..." : "Save changes"}
              </button>
            </div>
          </div>

          {/* Status panel */}
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <h3 className="text-sm font-semibold text-[#2C2C2A] mb-3">Status</h3>
            <div className="space-y-3 text-sm">
              <div className="flex items-center justify-between gap-3">
                <span className="text-[#5F5E5A]">Provider</span>
                <span className="font-medium text-[#2C2C2A] capitalize">
                  {emailStatus?.provider || (emailStatusLoaded ? "Resend" : "—")}
                </span>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-[#5F5E5A]">Sending domain</span>
                <span className="font-medium text-[#2C2C2A] truncate max-w-[60%] text-right">
                  {emailStatus?.domain
                    ? emailStatus.domain
                    : emailStatusLoaded
                      ? "Not configured yet — emails will be queued"
                      : "—"}
                </span>
              </div>
              <div className="flex items-center justify-between gap-3">
                <span className="text-[#5F5E5A]">Status</span>
                {emailStatusLoaded ? (
                  emailStatus?.configured ? (
                    <span className="text-[10px] font-medium px-2.5 py-1 rounded-full bg-[#E6F5ED] text-[#1D9E75]">
                      Active
                    </span>
                  ) : (
                    <span className="text-[10px] font-medium px-2.5 py-1 rounded-full bg-[#FDF3E7] text-[#BA7517]">
                      Pending setup
                    </span>
                  )
                ) : (
                  <span className="text-xs text-[#9E9C95]">Checking…</span>
                )}
              </div>
            </div>
          </div>

          {/* Advanced — Connect Gmail (opt-in legacy path) */}
          <div className="bg-white rounded-xl border border-[#E0DED8]">
            <button
              type="button"
              onClick={() => setEmailGmailAdvancedOpen((v) => !v)}
              className="w-full flex items-center justify-between gap-3 px-5 py-4 text-left hover:bg-[#F8F8F6] transition-colors rounded-xl"
              aria-expanded={emailGmailAdvancedOpen}
            >
              <div>
                <p className="text-sm font-semibold text-[#2C2C2A]">Advanced — Send from your own Gmail</p>
                <p className="text-xs text-[#5F5E5A] mt-0.5">
                  Optional. Connect a personal Gmail account to send from that address instead of ARIA&apos;s managed sender.
                </p>
              </div>
              <svg
                className={`w-4 h-4 text-[#5F5E5A] shrink-0 transition-transform ${emailGmailAdvancedOpen ? "rotate-180" : ""}`}
                fill="none"
                stroke="currentColor"
                strokeWidth={2}
                viewBox="0 0 24 24"
              >
                <path strokeLinecap="round" strokeLinejoin="round" d="M19 9l-7 7-7-7" />
              </svg>
            </button>

            {emailGmailAdvancedOpen && (
              <div className="border-t border-[#E0DED8]">
                <div className="px-5 py-4 flex items-center justify-between gap-3">
                  <div className="flex items-center gap-3 flex-1 min-w-0">
                    <div className="w-9 h-9 rounded-lg bg-[#FDF3E7] flex items-center justify-center shrink-0">
                      <svg className="w-5 h-5 text-[#BA7517]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                        <path strokeLinecap="round" strokeLinejoin="round" d="M21.75 6.75v10.5a2.25 2.25 0 01-2.25 2.25h-15a2.25 2.25 0 01-2.25-2.25V6.75m19.5 0A2.25 2.25 0 0019.5 4.5h-15a2.25 2.25 0 00-2.25 2.25m19.5 0v.243a2.25 2.25 0 01-1.07 1.916l-7.5 4.615a2.25 2.25 0 01-2.36 0L3.32 8.91a2.25 2.25 0 01-1.07-1.916V6.75" />
                      </svg>
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-[#2C2C2A]">Gmail</p>
                      <p className="text-xs text-[#5F5E5A] mt-0.5 break-words">
                        {gmailConnected === null ? "Checking..." : gmailConnected
                          ? `Connected — emails sent from ${gmailEmail || user.email}`
                          : "Not connected — emails will send from your ARIA managed address"}
                      </p>
                    </div>
                  </div>
                  {gmailConnected === false ? (
                    <button
                      onClick={reconnectGmail}
                      disabled={gmailReconnecting}
                      className="text-xs font-medium px-4 py-2 rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors disabled:opacity-60 shrink-0 whitespace-nowrap"
                    >
                      {gmailReconnecting ? "Connecting..." : "Connect Gmail"}
                    </button>
                  ) : gmailConnected ? (
                    <div className="flex items-center gap-2 shrink-0">
                      <span className="text-[10px] font-medium px-2.5 py-1 rounded-full bg-[#E6F5ED] text-[#1D9E75]">Connected</span>
                      <button onClick={reconnectGmail} className="text-[10px] text-[#534AB7] hover:underline transition-colors">Reconnect</button>
                      <button onClick={disconnectGmail} className="text-[10px] text-[#5F5E5A] hover:text-[#D85A30] transition-colors">Disconnect</button>
                    </div>
                  ) : null}
                </div>
                {gmailConnected === false && (
                  <div className="px-5 pb-4 -mt-1">
                    <p className="text-[11px] text-[#8A8983] leading-relaxed">
                      <span className="font-medium text-[#5F5E5A]">Tip:</span> when the Google account picker
                      appears, choose a <span className="font-medium">personal Gmail account</span>.
                      Work or school accounts (Google Workspace) usually have third-party Gmail
                      access disabled by their admin and will return an &ldquo;Access blocked&rdquo; error.
                    </p>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* Notifications */}
      {activeTab === "Notifications" && (
        <div className="bg-white rounded-xl border border-[#E0DED8] p-6 space-y-4">
          <h2 className="text-base font-semibold text-[#2C2C2A]">Notification Preferences</h2>
          {[
            { label: "Agent completes a task", desc: "Get notified when content is ready for review" },
            { label: "Weekly summary", desc: "Receive a weekly digest of all agent activity" },
            { label: "Strategy recommendations", desc: "CEO agent suggests changes to your GTM plan" },
          ].map((pref) => (
            <div key={pref.label} className="flex items-center justify-between py-2">
              <div>
                <p className="text-sm font-medium text-[#2C2C2A]">{pref.label}</p>
                <p className="text-xs text-[#5F5E5A]">{pref.desc}</p>
              </div>
              <button className="relative inline-flex h-5 w-9 items-center rounded-full bg-[#1D9E75] transition-colors">
                <span className="inline-block h-3.5 w-3.5 rounded-full bg-white translate-x-[18px] transition-transform" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Billing */}
      {activeTab === "Billing" && (
        <div className="space-y-6">
          <div className="bg-white rounded-xl border border-[#E0DED8] p-6">
            <div className="flex items-center justify-between mb-4">
              <div>
                <h2 className="text-base font-semibold text-[#2C2C2A]">Current Plan</h2>
                <p className="text-sm text-[#5F5E5A] mt-1">You&apos;re on the <span className="font-semibold text-[#534AB7]">Growth</span> plan</p>
              </div>
              <span className="text-2xl font-bold text-[#2C2C2A]">$149<span className="text-sm font-normal text-[#5F5E5A]">/mo</span></span>
            </div>
            <div className="grid grid-cols-3 gap-4 mt-4">
              {[
                { label: "Content pieces", used: 18, total: 30 },
                { label: "Email sequences", used: 2, total: 3 },
                { label: "Campaign plans", used: 1, total: 3 },
              ].map((u) => (
                <div key={u.label}>
                  <p className="text-xs text-[#5F5E5A] mb-1">{u.label}</p>
                  <div className="h-2 bg-[#F8F8F6] rounded-full overflow-hidden">
                    <div className="h-full bg-[#534AB7] rounded-full" style={{ width: `${(u.used / u.total) * 100}%` }} />
                  </div>
                  <p className="text-[10px] text-[#5F5E5A] mt-1">{u.used} / {u.total} used</p>
                </div>
              ))}
            </div>
            <button className="mt-4 text-xs font-medium text-[#534AB7] hover:underline">Upgrade to Scale ($299/mo)</button>
          </div>
        </div>
      )}
    </div>
  );
}
