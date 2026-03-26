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

export const analytics = {
  getData: (tenantId: string, dateRange: string) => fetchAPI(`/api/analytics/${tenantId}?date_range=${dateRange}`),
};
