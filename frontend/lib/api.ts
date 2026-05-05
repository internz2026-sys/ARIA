import { supabase } from "@/lib/supabase";

export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function getAuthHeaders(): Promise<Record<string, string>> {
  try {
    const { data: { session } } = await supabase.auth.getSession();
    if (session?.access_token) {
      return { Authorization: `Bearer ${session.access_token}` };
    }
  } catch {}
  return {};
}

/** Authenticated fetch wrapper for direct URL calls (use fetchAPI for /api endpoints) */
export async function authFetch(url: string, options?: RequestInit): Promise<Response> {
  const authHeaders = await getAuthHeaders();
  const headers: Record<string, string> = { "Content-Type": "application/json", ...authHeaders, ...options?.headers as Record<string, string> };
  return fetch(url, { ...options, headers });
}

async function fetchAPI(endpoint: string, options?: RequestInit) {
  const authHeaders = await getAuthHeaders();
  const res = await fetch(`${API_URL}${endpoint}`, {
    headers: { "Content-Type": "application/json", ...authHeaders, ...options?.headers },
    ...options,
  });
  if (!res.ok) {
    let detail = "";
    try {
      const body = await res.json();
      detail = body.detail || body.message || JSON.stringify(body);
    } catch { detail = res.statusText; }
    throw new Error(detail || `API error: ${res.status}`);
  }
  return res.json();
}

export const onboarding = {
  start: () => fetchAPI("/api/onboarding/start", { method: "POST", body: JSON.stringify({}) }),
  sendMessage: (sessionId: string, message: string) =>
    fetchAPI("/api/onboarding/message", { method: "POST", body: JSON.stringify({ session_id: sessionId, message }) }),
  extractConfig: (sessionId: string) =>
    fetchAPI("/api/onboarding/extract-config", { method: "POST", body: JSON.stringify({ session_id: sessionId }) }),
};

export const agents = {
  list: (tenantId: string) => fetchAPI(`/api/agents/${tenantId}`),
  run: (tenantId: string, agentName: string) => fetchAPI(`/api/agents/${tenantId}/${agentName}/run`, { method: "POST" }),
  pause: (tenantId: string, agentName: string) => fetchAPI(`/api/agents/${tenantId}/${agentName}/pause`, { method: "POST" }),
  resume: (tenantId: string, agentName: string) => fetchAPI(`/api/agents/${tenantId}/${agentName}/resume`, { method: "POST" }),
};

export const dashboard = {
  getStats: (tenantId: string) => fetchAPI(`/api/dashboard/${tenantId}/stats`),
  getActivity: (tenantId: string) => fetchAPI(`/api/dashboard/${tenantId}/activity`),
  getInbox: (tenantId: string) => fetchAPI(`/api/dashboard/${tenantId}/inbox`),
};

export const inbox = {
  counts: (tenantId: string) => fetchAPI(`/api/inbox/${tenantId}/counts`),
  /** Fetch a single inbox row by id — uses the existing deep-link hydrator. */
  getItem: (itemId: string) => fetchAPI(`/api/inbox/item/${encodeURIComponent(itemId)}`),
  list: (tenantId: string, status?: string, page: number = 1, pageSize: number = 20) => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    return fetchAPI(`/api/inbox/${tenantId}?${params.toString()}`);
  },
  update: (itemId: string, updates: { status: string }) =>
    fetchAPI(`/api/inbox/${itemId}`, { method: "PATCH", body: JSON.stringify(updates) }),
  updateItem: (
    itemId: string,
    updates: {
      title?: string;
      content?: string;
      metadata?: Record<string, unknown>;
      social_posts?: Array<{ platform: string; text: string; hashtags?: string[]; image_url?: string }>;
      email_draft?: Record<string, unknown>;
      status?: string;
    },
  ) => fetchAPI(`/api/inbox/${itemId}`, { method: "PATCH", body: JSON.stringify(updates) }),
  remove: (itemId: string) =>
    fetchAPI(`/api/inbox/${itemId}`, { method: "DELETE" }),
  approveSend: (tenantId: string, inboxItemId: string) =>
    fetchAPI(`/api/email/${tenantId}/approve-send`, {
      method: "POST",
      body: JSON.stringify({ inbox_item_id: inboxItemId }),
    }),
  cancelDraft: (tenantId: string, inboxItemId: string, reason?: string) =>
    fetchAPI(`/api/email/${tenantId}/cancel-draft`, {
      method: "POST",
      body: JSON.stringify({ inbox_item_id: inboxItemId, reason: reason || "" }),
    }),
  updateDraft: (tenantId: string, inboxItemId: string, updates: { to?: string; subject?: string; html_body?: string }) =>
    fetchAPI(`/api/email/${tenantId}/update-draft`, {
      method: "POST",
      body: JSON.stringify({ inbox_item_id: inboxItemId, ...updates }),
    }),
  approvePublishSocial: (tenantId: string, inboxItemId: string) =>
    fetchAPI(`/api/social/${tenantId}/approve-publish`, {
      method: "POST",
      body: JSON.stringify({ inbox_item_id: inboxItemId }),
    }),
};

export const emailThreads = {
  list: (tenantId: string, status?: string) =>
    fetchAPI(`/api/email/${tenantId}/threads${status ? `?status=${status}` : ""}`),
  get: (tenantId: string, threadId: string) =>
    fetchAPI(`/api/email/${tenantId}/threads/${threadId}`),
  markRead: (tenantId: string, threadId: string) =>
    fetchAPI(`/api/email/${tenantId}/threads/${threadId}/mark-read`, { method: "POST" }),
  draftReply: (tenantId: string, threadId: string, customInstructions?: string) =>
    fetchAPI(`/api/email/${tenantId}/draft-reply`, {
      method: "POST",
      body: JSON.stringify({ thread_id: threadId, custom_instructions: customInstructions || "" }),
    }),
  sendReply: (tenantId: string, threadId: string, body: string, subject?: string) =>
    fetchAPI(`/api/email/${tenantId}/threads/${threadId}/send-reply`, {
      method: "POST",
      body: JSON.stringify({ body, subject: subject || "" }),
    }),
  sync: (tenantId: string) =>
    fetchAPI(`/api/email/${tenantId}/sync`, { method: "POST" }),
};

export const analytics = {
  getData: (tenantId: string, dateRange: string) => fetchAPI(`/api/analytics/${tenantId}?date_range=${dateRange}`),
};

// ── CRM Types ──
interface CrmContactData { name: string; email?: string; phone?: string; company_id?: string; source?: string; status?: string; tags?: string[]; notes?: string }
interface CrmCompanyData { name: string; domain?: string; industry?: string; size?: string; notes?: string }
interface CrmDealData { title: string; value?: number; stage?: string; contact_id?: string; company_id?: string; notes?: string; expected_close?: string }

export const crm = {
  // Contacts
  listContacts: (tenantId: string, search = "", status = "", page = 1, pageSize = 50) => {
    const p = new URLSearchParams();
    if (search) p.set("search", search);
    if (status) p.set("status", status);
    p.set("page", String(page));
    p.set("page_size", String(pageSize));
    return fetchAPI(`/api/crm/${tenantId}/contacts?${p.toString()}`);
  },
  getContact: (tenantId: string, id: string) => fetchAPI(`/api/crm/${tenantId}/contacts/${id}`),
  createContact: (tenantId: string, data: CrmContactData) =>
    fetchAPI(`/api/crm/${tenantId}/contacts`, { method: "POST", body: JSON.stringify(data) }),
  updateContact: (tenantId: string, id: string, data: Partial<CrmContactData>) =>
    fetchAPI(`/api/crm/${tenantId}/contacts/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteContact: (tenantId: string, id: string) =>
    fetchAPI(`/api/crm/${tenantId}/contacts/${id}`, { method: "DELETE" }),
  sendEmailToContact: (tenantId: string, contactId: string, data: { subject: string; body: string }) =>
    fetchAPI(`/api/crm/${tenantId}/contacts/${contactId}/send-email`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  // Companies
  listCompanies: (tenantId: string, search = "") =>
    fetchAPI(`/api/crm/${tenantId}/companies${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  getCompany: (tenantId: string, id: string) => fetchAPI(`/api/crm/${tenantId}/companies/${id}`),
  createCompany: (tenantId: string, data: CrmCompanyData) =>
    fetchAPI(`/api/crm/${tenantId}/companies`, { method: "POST", body: JSON.stringify(data) }),
  updateCompany: (tenantId: string, id: string, data: Partial<CrmCompanyData>) =>
    fetchAPI(`/api/crm/${tenantId}/companies/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteCompany: (tenantId: string, id: string) =>
    fetchAPI(`/api/crm/${tenantId}/companies/${id}`, { method: "DELETE" }),

  // Deals
  listDeals: (tenantId: string, stage = "") =>
    fetchAPI(`/api/crm/${tenantId}/deals${stage ? `?stage=${stage}` : ""}`),
  getDeal: (tenantId: string, id: string) => fetchAPI(`/api/crm/${tenantId}/deals/${id}`),
  createDeal: (tenantId: string, data: CrmDealData) =>
    fetchAPI(`/api/crm/${tenantId}/deals`, { method: "POST", body: JSON.stringify(data) }),
  updateDeal: (tenantId: string, id: string, data: Partial<CrmDealData>) =>
    fetchAPI(`/api/crm/${tenantId}/deals/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteDeal: (tenantId: string, id: string) =>
    fetchAPI(`/api/crm/${tenantId}/deals/${id}`, { method: "DELETE" }),

  // Activities
  listActivities: (tenantId: string, contactId = "", limit = 30) => {
    const p = new URLSearchParams();
    if (contactId) p.set("contact_id", contactId);
    p.set("limit", String(limit));
    return fetchAPI(`/api/crm/${tenantId}/activities?${p.toString()}`);
  },
  createActivity: (tenantId: string, data: any) =>
    fetchAPI(`/api/crm/${tenantId}/activities`, { method: "POST", body: JSON.stringify(data) }),

  // Pipeline
  pipelineSummary: (tenantId: string) => fetchAPI(`/api/crm/${tenantId}/pipeline-summary`),
};

export const whatsapp = {
  connect: (tenantId: string, data: { access_token: string; phone_number_id: string; business_account_id?: string }) =>
    fetchAPI(`/api/whatsapp/${tenantId}/connect`, { method: "POST", body: JSON.stringify(data) }),
  disconnect: (tenantId: string) =>
    fetchAPI(`/api/whatsapp/${tenantId}/disconnect`, { method: "POST" }),
  send: (tenantId: string, to: string, message: string) =>
    fetchAPI(`/api/whatsapp/${tenantId}/send`, { method: "POST", body: JSON.stringify({ to, message }) }),
};

export const ceoActions = {
  execute: (tenantId: string, action: string, params: Record<string, any> = {}, confirmed = false) =>
    fetchAPI(`/api/ceo/${tenantId}/action`, {
      method: "POST",
      body: JSON.stringify({ action, params, confirmed }),
    }),
};

export const ceoChat = {
  /** List saved sessions for the sidebar/history picker. */
  listSessions: (tenantId: string) =>
    fetchAPI(`/api/ceo/chat/sessions/${tenantId}`),
  /** Hard-delete a session + its messages (cascade). Idempotent on 404. */
  deleteSession: (tenantId: string, sessionId: string) =>
    fetchAPI(`/api/ceo/chat/sessions/${tenantId}/${sessionId}`, { method: "DELETE" }),
  /** Bulk-delete many sessions in one round-trip. Returns {deleted, deleted_ids}. */
  deleteSessions: (tenantId: string, sessionIds: string[]) =>
    fetchAPI(`/api/ceo/chat/sessions/${tenantId}/bulk-delete`, {
      method: "POST",
      body: JSON.stringify({ session_ids: sessionIds }),
    }),
};

export const usage = {
  getDashboard: (tenantId: string) => fetchAPI(`/api/usage/${tenantId}`),
};

// ── Email sending config (Resend-managed sender identity) ──
//
// These endpoints are added by the backend Email coder. If they
// don't exist yet, both calls reject and the Settings → Email tab
// degrades gracefully (the UI stubs the save with a toast and shows
// an "integration in progress" status panel).
export interface EmailSettingsStatus {
  provider: string;          // e.g. "resend"
  domain: string;            // sending domain ("send.example.com") or ""
  configured: boolean;       // true once domain is verified + provider key is set
  display_name?: string;
  sender_address?: string;   // {slug}@send.{domain}
  reply_to_address?: string; // replies+{tenant_id}@inbound.{domain}
}

export interface EmailSettingsUpdate {
  display_name?: string;
}

export const email = {
  getStatus: (tenantId: string) =>
    fetchAPI(`/api/settings/${tenantId}/email/status`) as Promise<EmailSettingsStatus>,
  updateConfig: (tenantId: string, body: EmailSettingsUpdate) =>
    fetchAPI(`/api/settings/${tenantId}/email`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }) as Promise<EmailSettingsStatus>,
};

export const campaigns = {
  list: (tenantId: string, status = "", platform = "") => {
    const p = new URLSearchParams();
    if (status) p.set("status", status);
    if (platform) p.set("platform", platform);
    return fetchAPI(`/api/campaigns/${tenantId}?${p.toString()}`);
  },
  get: (tenantId: string, id: string) => fetchAPI(`/api/campaigns/${tenantId}/${id}`),
  create: (tenantId: string, data: any) =>
    fetchAPI(`/api/campaigns/${tenantId}`, { method: "POST", body: JSON.stringify(data) }),
  update: (tenantId: string, id: string, data: any) =>
    fetchAPI(`/api/campaigns/${tenantId}/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  /**
   * Convenience: write a `metadata.performance` block. Backend (Coder 2's
   * `update_campaign_metrics` helper, exposed via the existing
   * UpdateCampaignBody.metadata field) shallow-merges this into the
   * existing campaigns.metadata, so other keys (pasted_at, snapshot,
   * winning_variant, etc.) are preserved.
   */
  updateMetrics: (
    tenantId: string,
    id: string,
    perf: {
      clicks?: number | null;
      leads?: number | null;
      spend?: number | null;
      ctr?: number | null;
      cpl?: number | null;
      notes?: string;
    },
  ) =>
    fetchAPI(`/api/campaigns/${tenantId}/${id}`, {
      method: "PATCH",
      body: JSON.stringify({
        metadata: {
          performance: { ...perf, recorded_at: new Date().toISOString() },
        },
      }),
    }),
  /**
   * Convenience: status state-machine transitions. The Coder 1
   * whitelist enforces the value set on the backend
   * (draft/active/paused/completed/archived); anything else 400's.
   */
  updateStatus: (tenantId: string, id: string, status: string) =>
    fetchAPI(`/api/campaigns/${tenantId}/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  /**
   * Convenience: stamp the chosen A/B winner into metadata.
   */
  updateWinningVariant: (tenantId: string, id: string, winner: "A" | "B" | "tie") =>
    fetchAPI(`/api/campaigns/${tenantId}/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ metadata: { winning_variant: winner } }),
    }),
  delete: (tenantId: string, id: string) =>
    fetchAPI(`/api/campaigns/${tenantId}/${id}`, { method: "DELETE" }),
  listReports: (tenantId: string, campaignId: string) =>
    fetchAPI(`/api/campaigns/${tenantId}/${campaignId}/reports`),
  getReport: (tenantId: string, reportId: string) =>
    fetchAPI(`/api/campaigns/${tenantId}/reports/${reportId}`),
  generateAiReport: (tenantId: string, reportId: string) =>
    fetchAPI(`/api/campaigns/${tenantId}/reports/${reportId}/generate-ai-report`, { method: "POST" }),
  upload: async (tenantId: string, file: File, campaignId?: string) => {
    const authHeaders = await getAuthHeaders();
    const form = new FormData();
    form.append("file", file);
    if (campaignId) form.append("campaign_id", campaignId);
    const res = await fetch(`${API_URL}/api/campaigns/${tenantId}/upload`, {
      method: "POST",
      headers: { ...authHeaders },
      body: form,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Upload failed: ${res.status}`);
    }
    return res.json();
  },
  uploadAndCreate: async (tenantId: string, file: File, campaignName = "", platform = "facebook") => {
    const authHeaders = await getAuthHeaders();
    const form = new FormData();
    form.append("file", file);
    if (campaignName) form.append("campaign_name", campaignName);
    form.append("platform", platform);
    const res = await fetch(`${API_URL}/api/campaigns/${tenantId}/upload-and-create`, {
      method: "POST",
      headers: { ...authHeaders },
      body: form,
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `Upload failed: ${res.status}`);
    }
    return res.json();
  },
};

export const notificationsApi = {
  counts: (tenantId: string) => fetchAPI(`/api/notifications/${tenantId}/counts`),
  list: (tenantId: string, unreadOnly = false, limit = 30) =>
    fetchAPI(`/api/notifications/${tenantId}?unread_only=${unreadOnly}&limit=${limit}`),
  markRead: (tenantId: string, ids?: string[]) =>
    fetchAPI(`/api/notifications/${tenantId}/mark-read`, {
      method: "POST",
      body: JSON.stringify({ ids: ids || [] }),
    }),
  markSeen: (tenantId: string, ids?: string[]) =>
    fetchAPI(`/api/notifications/${tenantId}/mark-seen`, {
      method: "POST",
      body: JSON.stringify({ ids: ids || [] }),
    }),
};
