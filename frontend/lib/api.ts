export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

async function fetchAPI(endpoint: string, options?: RequestInit) {
  const res = await fetch(`${API_URL}${endpoint}`, {
    headers: { "Content-Type": "application/json", ...options?.headers },
    ...options,
  });
  if (!res.ok) throw new Error(`API error: ${res.status}`);
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
