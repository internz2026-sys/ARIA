import { supabase } from "@/lib/supabase";

export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function getAuthHeaders(): Promise<Record<string, string>> {
  try {
    const { data: { session } } = await supabase.auth.getSession();
    if (session?.access_token) {
      return { Authorization: `Bearer ${session.access_token}` };
    }
  } catch {}
  return {};
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
  list: (tenantId: string, status?: string, page: number = 1, pageSize: number = 20) => {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    return fetchAPI(`/api/inbox/${tenantId}?${params.toString()}`);
  },
  update: (itemId: string, updates: { status: string }) =>
    fetchAPI(`/api/inbox/${itemId}`, { method: "PATCH", body: JSON.stringify(updates) }),
  remove: (itemId: string) =>
    fetchAPI(`/api/inbox/${itemId}`, { method: "DELETE" }),
  approveSend: (tenantId: string, inboxItemId: string) =>
    fetchAPI(`/api/email/${tenantId}/approve-send`, {
      method: "POST",
      body: JSON.stringify({ inbox_item_id: inboxItemId }),
    }),
  cancelDraft: (tenantId: string, inboxItemId: string) =>
    fetchAPI(`/api/email/${tenantId}/cancel-draft`, {
      method: "POST",
      body: JSON.stringify({ inbox_item_id: inboxItemId }),
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
  sync: (tenantId: string) =>
    fetchAPI(`/api/email/${tenantId}/sync`, { method: "POST" }),
};

export const analytics = {
  getData: (tenantId: string, dateRange: string) => fetchAPI(`/api/analytics/${tenantId}?date_range=${dateRange}`),
};

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
  createContact: (tenantId: string, data: any) =>
    fetchAPI(`/api/crm/${tenantId}/contacts`, { method: "POST", body: JSON.stringify(data) }),
  updateContact: (tenantId: string, id: string, data: any) =>
    fetchAPI(`/api/crm/${tenantId}/contacts/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteContact: (tenantId: string, id: string) =>
    fetchAPI(`/api/crm/${tenantId}/contacts/${id}`, { method: "DELETE" }),

  // Companies
  listCompanies: (tenantId: string, search = "") =>
    fetchAPI(`/api/crm/${tenantId}/companies${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  getCompany: (tenantId: string, id: string) => fetchAPI(`/api/crm/${tenantId}/companies/${id}`),
  createCompany: (tenantId: string, data: any) =>
    fetchAPI(`/api/crm/${tenantId}/companies`, { method: "POST", body: JSON.stringify(data) }),
  updateCompany: (tenantId: string, id: string, data: any) =>
    fetchAPI(`/api/crm/${tenantId}/companies/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  deleteCompany: (tenantId: string, id: string) =>
    fetchAPI(`/api/crm/${tenantId}/companies/${id}`, { method: "DELETE" }),

  // Deals
  listDeals: (tenantId: string, stage = "") =>
    fetchAPI(`/api/crm/${tenantId}/deals${stage ? `?stage=${stage}` : ""}`),
  getDeal: (tenantId: string, id: string) => fetchAPI(`/api/crm/${tenantId}/deals/${id}`),
  createDeal: (tenantId: string, data: any) =>
    fetchAPI(`/api/crm/${tenantId}/deals`, { method: "POST", body: JSON.stringify(data) }),
  updateDeal: (tenantId: string, id: string, data: any) =>
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

export const usage = {
  getDashboard: (tenantId: string) => fetchAPI(`/api/usage/${tenantId}`),
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
