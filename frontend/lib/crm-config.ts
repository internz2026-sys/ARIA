// CRM types and configuration for ARIA's lightweight CRM

export interface CrmContact {
  id: string;
  tenant_id: string;
  company_id: string | null;
  name: string;
  email: string;
  phone: string;
  source: string;
  status: string;
  tags: string[];
  notes: string;
  created_at: string;
  updated_at: string;
  company_name?: string;
}

export interface CrmCompany {
  id: string;
  tenant_id: string;
  name: string;
  domain: string;
  industry: string;
  size: string;
  notes: string;
  created_at: string;
  updated_at: string;
  contact_count?: number;
}

export interface CrmDeal {
  id: string;
  tenant_id: string;
  contact_id: string | null;
  company_id: string | null;
  title: string;
  value: number;
  stage: string;
  notes: string;
  expected_close: string | null;
  created_at: string;
  updated_at: string;
  contact_name?: string;
  company_name?: string;
}

export interface CrmActivity {
  id: string;
  tenant_id: string;
  contact_id: string | null;
  deal_id: string | null;
  type: string;
  description: string;
  metadata: Record<string, any>;
  created_at: string;
}

export const CONTACT_STATUSES = [
  { key: "lead", label: "Lead", color: "#5F5E5A", bg: "#F8F8F6" },
  { key: "prospect", label: "Prospect", color: "#BA7517", bg: "#FDF3E7" },
  { key: "customer", label: "Customer", color: "#1D9E75", bg: "#E6F7F0" },
  { key: "churned", label: "Churned", color: "#D85A30", bg: "#FDEEE8" },
];

export const DEAL_STAGES = [
  { key: "lead", label: "Lead", color: "#5F5E5A" },
  { key: "qualified", label: "Qualified", color: "#534AB7" },
  { key: "proposal", label: "Proposal", color: "#BA7517" },
  { key: "negotiation", label: "Negotiation", color: "#D85A30" },
  { key: "won", label: "Won", color: "#1D9E75" },
  { key: "lost", label: "Lost", color: "#B0AFA8" },
];

export const CONTACT_SOURCES = ["manual", "email", "agent", "import", "website", "referral"];

export const COMPANY_SIZES = ["1-10", "11-50", "51-200", "201-500", "500+"];

export const ACTIVITY_TYPES: Record<string, { label: string; icon: string }> = {
  email_sent: { label: "Email sent", icon: "mail" },
  email_received: { label: "Email received", icon: "inbox" },
  note: { label: "Note", icon: "edit" },
  deal_created: { label: "Deal created", icon: "plus" },
  stage_changed: { label: "Stage changed", icon: "arrow" },
  contact_created: { label: "Contact created", icon: "user" },
  agent_interaction: { label: "Agent interaction", icon: "bot" },
};

export function formatCurrency(value: number): string {
  return new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", minimumFractionDigits: 0 }).format(value);
}

export function getStatusConfig(status: string) {
  return CONTACT_STATUSES.find(s => s.key === status) || CONTACT_STATUSES[0];
}

export function getStageConfig(stage: string) {
  return DEAL_STAGES.find(s => s.key === stage) || DEAL_STAGES[0];
}
