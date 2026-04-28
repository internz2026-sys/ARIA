"use client";

import React, { useState, useEffect, useCallback, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { crm } from "@/lib/api";
import {
  CrmContact, CrmCompany, CrmDeal,
  CONTACT_STATUSES, DEAL_STAGES, CONTACT_SOURCES, COMPANY_SIZES,
  getStageConfig, formatCurrency,
} from "@/lib/crm-config";
import { formatDateAgo as timeAgo } from "@/lib/utils";
import { useNotifications } from "@/lib/use-notifications";
import { useConfirm } from "@/lib/use-confirm";
import StatusDropdown from "@/components/shared/StatusDropdown";

// ─── Modal ───

function Modal({ open, onClose, title, children }: { open: boolean; onClose: () => void; title: string; children: React.ReactNode }) {
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/30" onClick={onClose} />
      <div className="relative bg-white rounded-xl border border-[#E0DED8] shadow-2xl w-full max-w-lg mx-4 max-h-[85vh] overflow-y-auto">
        <div className="flex items-center justify-between px-6 py-4 border-b border-[#E0DED8]">
          <h3 className="text-base font-semibold text-[#2C2C2A]">{title}</h3>
          <button onClick={onClose} className="text-[#9E9C95] hover:text-[#2C2C2A]">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
          </button>
        </div>
        <div className="p-6">{children}</div>
      </div>
    </div>
  );
}

function FormField({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <label className="text-xs font-semibold text-[#5F5E5A] uppercase">{label}</label>
      {children}
    </div>
  );
}

const inputCls = "w-full text-sm text-[#2C2C2A] bg-[#F8F8F6] border border-[#E0DED8] rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7]";
const selectCls = inputCls;

// ─── Main Page ───

type Tab = "contacts" | "companies" | "deals";

// Generic table sort state
type SortDir = "asc" | "desc";
interface SortState {
  key: string;
  dir: SortDir;
}

function sortRows<T extends Record<string, any>>(rows: T[], sort: SortState | null): T[] {
  if (!sort) return rows;
  const { key, dir } = sort;
  const mult = dir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const av = a[key];
    const bv = b[key];
    if (av == null && bv == null) return 0;
    if (av == null) return 1; // nulls last
    if (bv == null) return -1;
    if (typeof av === "number" && typeof bv === "number") return (av - bv) * mult;
    return String(av).localeCompare(String(bv)) * mult;
  });
}

function SortHeader({ label, sortKey, sort, onSort }: { label: string; sortKey: string; sort: SortState | null; onSort: (key: string) => void }) {
  const active = sort?.key === sortKey;
  return (
    <button
      onClick={() => onSort(sortKey)}
      className="flex items-center gap-1 text-left text-xs font-semibold text-[#5F5E5A] hover:text-[#2C2C2A] transition-colors w-full"
    >
      {label}
      {active && (
        <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth={2.5} viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" d={sort?.dir === "asc" ? "M5 15l7-7 7 7" : "M19 9l-7 7-7-7"} />
        </svg>
      )}
    </button>
  );
}

const CRM_PAGE_SIZE = 25;

function CrmPagination({ total, page, onPage }: { total: number; page: number; onPage: (p: number) => void }) {
  const totalPages = Math.max(1, Math.ceil(total / CRM_PAGE_SIZE));
  if (total <= CRM_PAGE_SIZE) return null;
  return (
    <div className="flex items-center justify-between px-4 py-3 border-t border-[#E0DED8] bg-[#F8F8F6]">
      <span className="text-xs text-[#5F5E5A]">
        Showing {(page - 1) * CRM_PAGE_SIZE + 1}–{Math.min(page * CRM_PAGE_SIZE, total)} of {total}
      </span>
      <div className="flex items-center gap-1">
        <button
          onClick={() => onPage(Math.max(1, page - 1))}
          disabled={page <= 1}
          className="px-2 py-1 text-xs rounded-md border border-[#E0DED8] text-[#5F5E5A] hover:bg-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          Prev
        </button>
        <span className="text-xs text-[#5F5E5A] px-2">Page {page} of {totalPages}</span>
        <button
          onClick={() => onPage(Math.min(totalPages, page + 1))}
          disabled={page >= totalPages}
          className="px-2 py-1 text-xs rounded-md border border-[#E0DED8] text-[#5F5E5A] hover:bg-white disabled:opacity-40 disabled:cursor-not-allowed transition-colors"
        >
          Next
        </button>
      </div>
    </div>
  );
}

export default function CRMPage() {
  const { showToast } = useNotifications();
  const { confirm } = useConfirm();
  const [tab, setTab] = useState<Tab>("contacts");

  // Per-tab sort + page state
  const [contactSort, setContactSort] = useState<SortState | null>({ key: "created_at", dir: "desc" });
  const [contactPage, setContactPage] = useState(1);
  const [companySort, setCompanySort] = useState<SortState | null>({ key: "created_at", dir: "desc" });
  const [companyPage, setCompanyPage] = useState(1);
  const [dealSort, setDealSort] = useState<SortState | null>({ key: "value", dir: "desc" });
  const [dealPage, setDealPage] = useState(1);

  function toggleSort(current: SortState | null, key: string): SortState {
    if (current?.key === key) {
      return { key, dir: current.dir === "asc" ? "desc" : "asc" };
    }
    return { key, dir: "asc" };
  }
  const tenantId = typeof window !== "undefined" ? localStorage.getItem("aria_tenant_id") || "" : "";

  // Deep-link support — a notification click that targets a CRM
  // resource (contact / company / deal) lands here with ?id=<uuid>
  // and optional &tab=<contacts|companies|deals>. Highlight the row
  // briefly and auto-switch to the right tab so the user sees the
  // item without manual searching.
  const searchParams = useSearchParams();
  const deepLinkId = searchParams?.get("id") || "";
  const deepLinkTab = searchParams?.get("tab") as Tab | null;
  const [highlightedId, setHighlightedId] = useState<string | null>(null);

  useEffect(() => {
    if (deepLinkTab && deepLinkTab !== tab) setTab(deepLinkTab);
  }, [deepLinkTab]);

  useEffect(() => {
    if (!deepLinkId) return;
    setHighlightedId(deepLinkId);
    const scrollTimer = requestAnimationFrame(() => {
      const el = document.querySelector(`[data-crm-row="${deepLinkId}"]`);
      if (el && typeof (el as any).scrollIntoView === "function") {
        (el as HTMLElement).scrollIntoView({ behavior: "smooth", block: "center" });
      }
    });
    const t = setTimeout(() => setHighlightedId(null), 1800);
    return () => {
      cancelAnimationFrame(scrollTimer);
      clearTimeout(t);
    };
  }, [deepLinkId]);

  // ─── Contacts state ───
  const [contacts, setContacts] = useState<CrmContact[]>([]);
  const [contactSearch, setContactSearch] = useState("");
  const [debouncedContactSearch, setDebouncedContactSearch] = useState("");
  const [contactFilter, setContactFilter] = useState("");
  const [contactLoading, setContactLoading] = useState(true);
  const [showAddContact, setShowAddContact] = useState(false);

  // ─── Companies state ───
  const [companies, setCompanies] = useState<CrmCompany[]>([]);
  const [companySearch, setCompanySearch] = useState("");
  const [debouncedCompanySearch, setDebouncedCompanySearch] = useState("");
  const [companyLoading, setCompanyLoading] = useState(true);
  const [showAddCompany, setShowAddCompany] = useState(false);

  // ─── Deals state ───
  const [deals, setDeals] = useState<CrmDeal[]>([]);
  const [dealLoading, setDealLoading] = useState(true);
  const [showAddDeal, setShowAddDeal] = useState(false);
  const [pipelineSummary, setPipelineSummary] = useState<Record<string, { count: number; value: number }>>({});

  // ─── Debounce search inputs (300ms) ───
  useEffect(() => {
    const t = setTimeout(() => setDebouncedContactSearch(contactSearch), 300);
    return () => clearTimeout(t);
  }, [contactSearch]);
  useEffect(() => {
    const t = setTimeout(() => setDebouncedCompanySearch(companySearch), 300);
    return () => clearTimeout(t);
  }, [companySearch]);

  // ─── Fetchers ───
  const fetchContacts = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await crm.listContacts(tenantId, debouncedContactSearch, contactFilter);
      setContacts(data.contacts || []);
    } catch {} finally { setContactLoading(false); }
  }, [tenantId, debouncedContactSearch, contactFilter]);

  const fetchCompanies = useCallback(async () => {
    if (!tenantId) return;
    try {
      const data = await crm.listCompanies(tenantId, debouncedCompanySearch);
      setCompanies(data.companies || []);
    } catch {} finally { setCompanyLoading(false); }
  }, [tenantId, debouncedCompanySearch]);

  const fetchDeals = useCallback(async () => {
    if (!tenantId) return;
    try {
      const [d, p] = await Promise.all([crm.listDeals(tenantId), crm.pipelineSummary(tenantId)]);
      setDeals(d.deals || []);
      setPipelineSummary(p.stages || {});
    } catch {} finally { setDealLoading(false); }
  }, [tenantId]);

  useEffect(() => { if (tab === "contacts") fetchContacts(); }, [tab, fetchContacts]);
  useEffect(() => { if (tab === "companies") fetchCompanies(); }, [tab, fetchCompanies]);
  useEffect(() => { if (tab === "deals") fetchDeals(); }, [tab, fetchDeals]);

  // Real-time refresh when CEO creates/updates CRM records (entity-targeted)
  useEffect(() => {
    if (!tenantId) return;
    try {
      const { getSocket } = require("@/lib/socket");
      const socket = getSocket();
      const handleCrmUpdate = (data: { entity?: string }) => {
        const entity = data?.entity || "";
        if (entity === "crm_contact" || !entity) fetchContacts();
        if (entity === "crm_company" || !entity) fetchCompanies();
        if (entity === "crm_deal" || !entity) fetchDeals();
      };
      socket.on("ceo_action_executed", handleCrmUpdate);
      return () => { socket.off("ceo_action_executed", handleCrmUpdate); };
    } catch {}
  }, [tenantId, fetchContacts, fetchCompanies, fetchDeals]);

  // ─── Contact CRUD ───
  const [newContact, setNewContact] = useState({ name: "", email: "", phone: "", source: "manual", status: "lead", notes: "" });

  async function handleCreateContact() {
    if (!newContact.name.trim()) return;
    try {
      await crm.createContact(tenantId, newContact);
      setShowAddContact(false);
      const created = newContact.name;
      setNewContact({ name: "", email: "", phone: "", source: "manual", status: "lead", notes: "" });
      fetchContacts();
      showToast({ title: `Contact added: ${created}`, variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't create contact",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  }

  async function handleDeleteContact(id: string) {
    const c = contacts.find((x) => x.id === id);
    const ok = await confirm({
      title: "Delete this contact?",
      message: c ? `${c.name} will be permanently removed from your CRM.` : "This contact will be permanently removed.",
      confirmLabel: "Delete",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    try {
      await crm.deleteContact(tenantId, id);
      fetchContacts();
      showToast({ title: "Contact deleted", variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't delete contact",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  }

  async function handleContactStatusChange(id: string, status: string) {
    try {
      await crm.updateContact(tenantId, id, { status });
      setContacts(prev => prev.map(c => c.id === id ? { ...c, status } : c));
      showToast({ title: `Status: ${status}`, variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't update status",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  }

  // ─── Company CRUD ───
  const [newCompany, setNewCompany] = useState({ name: "", domain: "", industry: "", size: "", notes: "" });

  async function handleCreateCompany() {
    if (!newCompany.name.trim()) return;
    try {
      await crm.createCompany(tenantId, newCompany);
      setShowAddCompany(false);
      const created = newCompany.name;
      setNewCompany({ name: "", domain: "", industry: "", size: "", notes: "" });
      fetchCompanies();
      showToast({ title: `Company added: ${created}`, variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't create company",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  }

  async function handleDeleteCompany(id: string) {
    const c = companies.find((x) => x.id === id);
    const ok = await confirm({
      title: "Delete this company?",
      message: c ? `${c.name} will be permanently removed.` : "This company will be permanently removed.",
      confirmLabel: "Delete",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    try {
      await crm.deleteCompany(tenantId, id);
      fetchCompanies();
      showToast({ title: "Company deleted", variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't delete company",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  }

  // ─── Deal CRUD ───
  const [newDeal, setNewDeal] = useState({ title: "", value: 0, stage: "lead", notes: "" });

  async function handleCreateDeal() {
    if (!newDeal.title.trim()) return;
    try {
      await crm.createDeal(tenantId, newDeal);
      setShowAddDeal(false);
      const created = newDeal.title;
      setNewDeal({ title: "", value: 0, stage: "lead", notes: "" });
      fetchDeals();
      showToast({ title: `Deal added: ${created}`, variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't create deal",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  }

  async function handleDealStageChange(id: string, stage: string) {
    try {
      await crm.updateDeal(tenantId, id, { stage });
      setDeals(prev => prev.map(d => d.id === id ? { ...d, stage } : d));
      // Refresh pipeline summary
      crm.pipelineSummary(tenantId).then(p => setPipelineSummary(p.stages || {})).catch(() => {});
      showToast({ title: `Moved to ${stage}`, variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't update deal",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  }

  async function handleDeleteDeal(id: string) {
    const d = deals.find((x) => x.id === id);
    const ok = await confirm({
      title: "Delete this deal?",
      message: d ? `"${d.title}" will be permanently removed from your pipeline.` : "This deal will be permanently removed.",
      confirmLabel: "Delete",
      cancelLabel: "Cancel",
      destructive: true,
    });
    if (!ok) return;
    try {
      await crm.deleteDeal(tenantId, id);
      fetchDeals();
      showToast({ title: "Deal deleted", variant: "success" });
    } catch (err: any) {
      showToast({
        title: "Couldn't delete deal",
        body: err?.message || "Network error -- please try again.",
        variant: "error",
      });
    }
  }

  const tabs: { key: Tab; label: string }[] = [
    { key: "contacts", label: "Contacts" },
    { key: "companies", label: "Companies" },
    { key: "deals", label: "Deals" },
  ];

  return (
    <div className="max-w-screen-2xl mx-auto space-y-4">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl sm:text-2xl font-semibold text-[#2C2C2A]">CRM</h1>
          <p className="text-sm text-[#5F5E5A]">Manage contacts, companies, and deals</p>
        </div>
        <button
          onClick={() => {
            if (tab === "contacts") setShowAddContact(true);
            else if (tab === "companies") setShowAddCompany(true);
            else setShowAddDeal(true);
          }}
          className="flex items-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors shrink-0 whitespace-nowrap"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M12 4.5v15m7.5-7.5h-15" /></svg>
          Add {tab === "contacts" ? "Contact" : tab === "companies" ? "Company" : "Deal"}
        </button>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 bg-white rounded-xl border border-[#E0DED8] p-1.5">
        {tabs.map(t => (
          <button key={t.key} onClick={() => setTab(t.key)}
            className={`px-4 py-2 rounded-lg text-sm font-medium transition-colors ${
              tab === t.key ? "bg-[#EEEDFE] text-[#534AB7]" : "text-[#5F5E5A] hover:bg-[#F8F8F6]"
            }`}
          >{t.label}</button>
        ))}
      </div>

      {/* ════════ CONTACTS TAB ════════ */}
      {tab === "contacts" && (
        <>
          {/* Search + filter */}
          <div className="flex items-center gap-3">
            <div className="flex-1 relative">
              <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[#9E9C95]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" /></svg>
              <input
                value={contactSearch} onChange={e => setContactSearch(e.target.value)}
                placeholder="Search contacts..."
                className="w-full pl-10 pr-4 py-2.5 text-sm bg-white border border-[#E0DED8] rounded-lg focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 text-[#2C2C2A] placeholder:text-[#6B6A65]"
              />
            </div>
            <select value={contactFilter} onChange={e => setContactFilter(e.target.value)}
              className="text-sm border border-[#E0DED8] rounded-lg px-3 py-2.5 bg-white text-[#5F5E5A] focus:outline-none"
            >
              <option value="">All statuses</option>
              {CONTACT_STATUSES.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
            </select>
          </div>

          {/* Table — wrapper is overflow-x-auto so the 7-column table
              can scroll horizontally on phones without forcing the whole
              page wider. The table itself sets min-w-[720px] so columns
              keep their natural width instead of squishing the email
              into one character per row. */}
          <div className="bg-white rounded-xl border border-[#E0DED8] overflow-x-auto">
            {contactLoading ? (
              <div className="p-8 text-center text-sm text-[#9E9C95]">Loading contacts...</div>
            ) : contacts.length === 0 ? (
              <div className="p-12 text-center">
                <p className="text-sm text-[#9E9C95]">No contacts yet. Add your first contact to get started.</p>
              </div>
            ) : (
              <table className="w-full min-w-[720px] text-sm">
                <thead>
                  <tr className="border-b border-[#E0DED8] bg-[#F8F8F6]">
                    <th className="text-left px-4 py-3"><SortHeader label="Name" sortKey="name" sort={contactSort} onSort={(k) => { setContactSort(toggleSort(contactSort, k)); setContactPage(1); }} /></th>
                    <th className="text-left px-4 py-3"><SortHeader label="Email" sortKey="email" sort={contactSort} onSort={(k) => { setContactSort(toggleSort(contactSort, k)); setContactPage(1); }} /></th>
                    <th className="text-left px-4 py-3 font-semibold text-[#5F5E5A]">Phone</th>
                    <th className="text-left px-4 py-3"><SortHeader label="Status" sortKey="status" sort={contactSort} onSort={(k) => { setContactSort(toggleSort(contactSort, k)); setContactPage(1); }} /></th>
                    <th className="text-left px-4 py-3"><SortHeader label="Source" sortKey="source" sort={contactSort} onSort={(k) => { setContactSort(toggleSort(contactSort, k)); setContactPage(1); }} /></th>
                    <th className="text-left px-4 py-3"><SortHeader label="Added" sortKey="created_at" sort={contactSort} onSort={(k) => { setContactSort(toggleSort(contactSort, k)); setContactPage(1); }} /></th>
                    <th className="px-4 py-3"></th>
                  </tr>
                </thead>
                <tbody>
                  {sortRows(contacts, contactSort).slice((contactPage - 1) * CRM_PAGE_SIZE, contactPage * CRM_PAGE_SIZE).map(c => {
                    return (
                      <tr
                        key={c.id}
                        data-crm-row={c.id}
                        className={`border-b border-[#F0EFEC] transition-colors ${
                          highlightedId === c.id
                            ? "bg-[#EEEDFE] ring-2 ring-inset ring-[#534AB7]/40 animate-pulse"
                            : "hover:bg-[#F8F8F6]"
                        }`}
                      >
                        <td className="px-4 py-3 font-medium text-[#2C2C2A]">{c.name}</td>
                        <td className="px-4 py-3 text-[#5F5E5A]">{c.email || "—"}</td>
                        <td className="px-4 py-3 text-[#5F5E5A]">{c.phone || "—"}</td>
                        <td className="px-4 py-3">
                          <StatusDropdown
                            value={c.status}
                            options={CONTACT_STATUSES}
                            onChange={(newStatus) => handleContactStatusChange(c.id, newStatus)}
                          />
                        </td>
                        <td className="px-4 py-3 text-[#9E9C95] text-xs">{c.source}</td>
                        <td className="px-4 py-3 text-[#9E9C95] text-xs">{timeAgo(c.created_at)}</td>
                        <td className="px-4 py-3">
                          <button onClick={() => handleDeleteContact(c.id)} className="text-[#B0AFA8] hover:text-red-500 transition-colors">
                            <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg>
                          </button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            )}
            <CrmPagination total={contacts.length} page={contactPage} onPage={setContactPage} />
          </div>
        </>
      )}

      {/* ════════ COMPANIES TAB ════════ */}
      {tab === "companies" && (
        <>
          <div className="flex items-center gap-3">
            <div className="flex-1 relative">
              <svg className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-[#9E9C95]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-5.197-5.197m0 0A7.5 7.5 0 105.196 5.196a7.5 7.5 0 0010.607 10.607z" /></svg>
              <input value={companySearch} onChange={e => setCompanySearch(e.target.value)} placeholder="Search companies..."
                className="w-full pl-10 pr-4 py-2.5 text-sm bg-white border border-[#E0DED8] rounded-lg focus:outline-none focus:ring-2 focus:ring-[#534AB7]/20 text-[#2C2C2A] placeholder:text-[#6B6A65]" />
            </div>
          </div>
          <div className="bg-white rounded-xl border border-[#E0DED8] overflow-x-auto">
            {companyLoading ? (
              <div className="p-8 text-center text-sm text-[#9E9C95]">Loading companies...</div>
            ) : companies.length === 0 ? (
              <div className="p-12 text-center"><p className="text-sm text-[#9E9C95]">No companies yet.</p></div>
            ) : (
              <table className="w-full min-w-[640px] text-sm">
                <thead>
                  <tr className="border-b border-[#E0DED8] bg-[#F8F8F6]">
                    <th className="text-left px-4 py-3"><SortHeader label="Name" sortKey="name" sort={companySort} onSort={(k) => { setCompanySort(toggleSort(companySort, k)); setCompanyPage(1); }} /></th>
                    <th className="text-left px-4 py-3"><SortHeader label="Domain" sortKey="domain" sort={companySort} onSort={(k) => { setCompanySort(toggleSort(companySort, k)); setCompanyPage(1); }} /></th>
                    <th className="text-left px-4 py-3"><SortHeader label="Industry" sortKey="industry" sort={companySort} onSort={(k) => { setCompanySort(toggleSort(companySort, k)); setCompanyPage(1); }} /></th>
                    <th className="text-left px-4 py-3"><SortHeader label="Size" sortKey="size" sort={companySort} onSort={(k) => { setCompanySort(toggleSort(companySort, k)); setCompanyPage(1); }} /></th>
                    <th className="text-left px-4 py-3"><SortHeader label="Added" sortKey="created_at" sort={companySort} onSort={(k) => { setCompanySort(toggleSort(companySort, k)); setCompanyPage(1); }} /></th>
                    <th className="px-4 py-3"></th>
                  </tr>
                </thead>
                <tbody>
                  {sortRows(companies, companySort).slice((companyPage - 1) * CRM_PAGE_SIZE, companyPage * CRM_PAGE_SIZE).map(c => (
                    <tr
                      key={c.id}
                      data-crm-row={c.id}
                      className={`border-b border-[#F0EFEC] transition-colors ${
                        highlightedId === c.id
                          ? "bg-[#EEEDFE] ring-2 ring-inset ring-[#534AB7]/40 animate-pulse"
                          : "hover:bg-[#F8F8F6]"
                      }`}
                    >
                      <td className="px-4 py-3 font-medium text-[#2C2C2A]">{c.name}</td>
                      <td className="px-4 py-3 text-[#534AB7]">{c.domain || "—"}</td>
                      <td className="px-4 py-3 text-[#5F5E5A]">{c.industry || "—"}</td>
                      <td className="px-4 py-3 text-[#5F5E5A]">{c.size || "—"}</td>
                      <td className="px-4 py-3 text-[#9E9C95] text-xs">{timeAgo(c.created_at)}</td>
                      <td className="px-4 py-3">
                        <button onClick={() => handleDeleteCompany(c.id)} className="text-[#B0AFA8] hover:text-red-500 transition-colors">
                          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M14.74 9l-.346 9m-4.788 0L9.26 9m9.968-3.21c.342.052.682.107 1.022.166m-1.022-.165L18.16 19.673a2.25 2.25 0 01-2.244 2.077H8.084a2.25 2.25 0 01-2.244-2.077L4.772 5.79m14.456 0a48.108 48.108 0 00-3.478-.397m-12 .562c.34-.059.68-.114 1.022-.165m0 0a48.11 48.11 0 013.478-.397m7.5 0v-.916c0-1.18-.91-2.164-2.09-2.201a51.964 51.964 0 00-3.32 0c-1.18.037-2.09 1.022-2.09 2.201v.916m7.5 0a48.667 48.667 0 00-7.5 0" /></svg>
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <CrmPagination total={companies.length} page={companyPage} onPage={setCompanyPage} />
          </div>
        </>
      )}

      {/* ════════ DEALS TAB (Pipeline Kanban) ════════ */}
      {tab === "deals" && (
        <>
          {/* Pipeline summary */}
          <div className="flex items-center gap-3 overflow-x-auto pb-1">
            {DEAL_STAGES.map(s => {
              const data = pipelineSummary[s.key] || { count: 0, value: 0 };
              return (
                <div key={s.key} className="flex items-center gap-2 px-3 py-2 bg-white rounded-lg border border-[#E0DED8] min-w-fit">
                  <span className="w-2 h-2 rounded-full" style={{ backgroundColor: s.color }} />
                  <span className="text-xs font-medium text-[#2C2C2A]">{s.label}</span>
                  <span className="text-xs text-[#9E9C95]">{data.count}</span>
                  {data.value > 0 && <span className="text-xs font-medium text-[#1D9E75]">{formatCurrency(data.value)}</span>}
                </div>
              );
            })}
          </div>

          {/* Kanban board */}
          {dealLoading ? (
            <div className="p-8 text-center text-sm text-[#9E9C95]">Loading deals...</div>
          ) : (
            <div className="flex flex-col md:flex-row gap-3 md:overflow-x-auto pb-4">
              {DEAL_STAGES.map(stage => {
                const stageDeals = deals.filter(d => d.stage === stage.key);
                return (
                  <div key={stage.key} className="w-full md:min-w-[260px] md:w-[260px] md:shrink-0">
                    {/* Column header */}
                    <div className="flex items-center gap-2 px-3 py-2.5 mb-2">
                      <span className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: stage.color }} />
                      <span className="text-xs font-semibold text-[#2C2C2A]">{stage.label}</span>
                      <span className="text-[10px] text-[#9E9C95] bg-[#F8F8F6] px-1.5 py-0.5 rounded-full">{stageDeals.length}</span>
                    </div>
                    {/* Cards */}
                    <div className="space-y-2 min-h-[100px]">
                      {stageDeals.map(deal => (
                        <div key={deal.id} className="bg-white rounded-xl border border-[#E0DED8] p-3.5 hover:shadow-sm transition-shadow">
                          <div className="flex items-start justify-between mb-2">
                            <p className="text-sm font-medium text-[#2C2C2A] flex-1">{deal.title}</p>
                            <button onClick={() => handleDeleteDeal(deal.id)} className="text-[#B0AFA8] hover:text-red-500 shrink-0 ml-2">
                              <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" /></svg>
                            </button>
                          </div>
                          {deal.value > 0 && (
                            <p className="text-sm font-semibold text-[#1D9E75] mb-2">{formatCurrency(deal.value)}</p>
                          )}
                          {deal.notes && <p className="text-[11px] text-[#9E9C95] mb-2 line-clamp-2">{deal.notes}</p>}
                          {/* Stage selector */}
                          <select value={deal.stage} onChange={e => handleDealStageChange(deal.id, e.target.value)}
                            className="w-full text-[11px] font-medium px-2 py-1.5 rounded-lg border border-[#E0DED8] bg-[#F8F8F6] text-[#5F5E5A] cursor-pointer focus:outline-none"
                          >
                            {DEAL_STAGES.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
                          </select>
                        </div>
                      ))}
                      {stageDeals.length === 0 && (
                        <div className="border-2 border-dashed border-[#E0DED8] rounded-xl p-4 text-center">
                          <p className="text-[11px] text-[#B0AFA8]">No deals</p>
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}

      {/* ════════ ADD CONTACT MODAL ════════ */}
      <Modal open={showAddContact} onClose={() => setShowAddContact(false)} title="Add Contact">
        <div className="space-y-4">
          <FormField label="Name *">
            <input value={newContact.name} onChange={e => setNewContact({ ...newContact, name: e.target.value })} className={inputCls} placeholder="Full name" />
          </FormField>
          <FormField label="Email">
            <input type="email" value={newContact.email} onChange={e => setNewContact({ ...newContact, email: e.target.value })} className={inputCls} placeholder="email@example.com" />
          </FormField>
          <FormField label="Phone">
            <input value={newContact.phone} onChange={e => setNewContact({ ...newContact, phone: e.target.value })} className={inputCls} placeholder="+1 234 567 8900" />
          </FormField>
          <div className="grid grid-cols-2 gap-4">
            <FormField label="Status">
              <select value={newContact.status} onChange={e => setNewContact({ ...newContact, status: e.target.value })} className={selectCls}>
                {CONTACT_STATUSES.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
              </select>
            </FormField>
            <FormField label="Source">
              <select value={newContact.source} onChange={e => setNewContact({ ...newContact, source: e.target.value })} className={selectCls}>
                {CONTACT_SOURCES.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </FormField>
          </div>
          <FormField label="Notes">
            <textarea value={newContact.notes} onChange={e => setNewContact({ ...newContact, notes: e.target.value })} className={inputCls} rows={3} placeholder="Any notes..." />
          </FormField>
          <button onClick={handleCreateContact} disabled={!newContact.name.trim()}
            className="w-full py-2.5 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors disabled:opacity-40"
          >Add Contact</button>
        </div>
      </Modal>

      {/* ════════ ADD COMPANY MODAL ════════ */}
      <Modal open={showAddCompany} onClose={() => setShowAddCompany(false)} title="Add Company">
        <div className="space-y-4">
          <FormField label="Company Name *">
            <input value={newCompany.name} onChange={e => setNewCompany({ ...newCompany, name: e.target.value })} className={inputCls} placeholder="Acme Inc." />
          </FormField>
          <FormField label="Domain">
            <input value={newCompany.domain} onChange={e => setNewCompany({ ...newCompany, domain: e.target.value })} className={inputCls} placeholder="acme.com" />
          </FormField>
          <div className="grid grid-cols-2 gap-4">
            <FormField label="Industry">
              <input value={newCompany.industry} onChange={e => setNewCompany({ ...newCompany, industry: e.target.value })} className={inputCls} placeholder="Technology" />
            </FormField>
            <FormField label="Size">
              <select value={newCompany.size} onChange={e => setNewCompany({ ...newCompany, size: e.target.value })} className={selectCls}>
                <option value="">Select size</option>
                {COMPANY_SIZES.map(s => <option key={s} value={s}>{s}</option>)}
              </select>
            </FormField>
          </div>
          <FormField label="Notes">
            <textarea value={newCompany.notes} onChange={e => setNewCompany({ ...newCompany, notes: e.target.value })} className={inputCls} rows={3} />
          </FormField>
          <button onClick={handleCreateCompany} disabled={!newCompany.name.trim()}
            className="w-full py-2.5 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors disabled:opacity-40"
          >Add Company</button>
        </div>
      </Modal>

      {/* ════════ ADD DEAL MODAL ════════ */}
      <Modal open={showAddDeal} onClose={() => setShowAddDeal(false)} title="Add Deal">
        <div className="space-y-4">
          <FormField label="Deal Title *">
            <input value={newDeal.title} onChange={e => setNewDeal({ ...newDeal, title: e.target.value })} className={inputCls} placeholder="Enterprise onboarding" />
          </FormField>
          <div className="grid grid-cols-2 gap-4">
            <FormField label="Value ($)">
              <input type="number" value={newDeal.value} onChange={e => setNewDeal({ ...newDeal, value: Number(e.target.value) })} className={inputCls} placeholder="0" />
            </FormField>
            <FormField label="Stage">
              <select value={newDeal.stage} onChange={e => setNewDeal({ ...newDeal, stage: e.target.value })} className={selectCls}>
                {DEAL_STAGES.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}
              </select>
            </FormField>
          </div>
          <FormField label="Notes">
            <textarea value={newDeal.notes} onChange={e => setNewDeal({ ...newDeal, notes: e.target.value })} className={inputCls} rows={3} />
          </FormField>
          <button onClick={handleCreateDeal} disabled={!newDeal.title.trim()}
            className="w-full py-2.5 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition-colors disabled:opacity-40"
          >Add Deal</button>
        </div>
      </Modal>
    </div>
  );
}
