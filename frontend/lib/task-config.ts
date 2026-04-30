// ---------------------------------------------------------------------------
// ARIA Task / Kanban shared config — used by Projects page & Office dropdown
// ---------------------------------------------------------------------------

export interface Task {
  id: string;
  agent: string;
  task: string;
  priority: string;
  status: string;
  created_at: string;
  updated_at: string;
}

export const STATUS_COLUMNS = [
  { key: "backlog", label: "Backlog", color: "#5F5E5A", bg: "#F8F8F6" },
  { key: "to_do", label: "To Do", color: "#534AB7", bg: "#EEEDFE" },
  { key: "in_progress", label: "In Progress", color: "#BA7517", bg: "#FDF3E7" },
  { key: "done", label: "Done", color: "#1D9E75", bg: "#E6F7F0" },
] as const;

export { AGENT_LABELS } from "./agent-config";

export const PRIORITY_STYLES: Record<string, { label: string; color: string; bg: string }> = {
  high: { label: "High", color: "#D85A30", bg: "#FEF2EE" },
  medium: { label: "Medium", color: "#BA7517", bg: "#FDF3E7" },
  low: { label: "Low", color: "#5F5E5A", bg: "#F8F8F6" },
};

// ---- API helpers -----------------------------------------------------------

import { API_URL, authFetch } from "./api";

export async function fetchTasks(tenantId: string): Promise<Task[]> {
  const res = await authFetch(`${API_URL}/api/tasks/${tenantId}`);
  const data = await res.json();
  return data.tasks || [];
}

export async function patchTaskStatus(taskId: string, status: string): Promise<void> {
  await authFetch(`${API_URL}/api/tasks/${taskId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ status }),
  });
}

export async function deleteTaskApi(taskId: string): Promise<void> {
  // Soft-delete since 2026-04-30 — sets deleted_at on the row instead
  // of issuing a real DELETE. Row drops out of the main list and shows
  // up in the Trash tab where it can be restored or permanently
  // removed.
  await authFetch(`${API_URL}/api/tasks/${taskId}`, { method: "DELETE" });
}

export async function fetchTrashedTasks(tenantId: string): Promise<Task[]> {
  const res = await authFetch(`${API_URL}/api/tasks/trash/${tenantId}`);
  const data = await res.json();
  return data.tasks || [];
}

export async function restoreTaskApi(taskId: string): Promise<void> {
  await authFetch(`${API_URL}/api/tasks/${taskId}/restore`, { method: "POST" });
}

export async function permanentDeleteTaskApi(taskId: string): Promise<void> {
  await authFetch(`${API_URL}/api/tasks/${taskId}/permanent`, { method: "DELETE" });
}
