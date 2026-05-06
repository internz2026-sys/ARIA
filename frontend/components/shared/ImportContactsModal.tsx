"use client";

/**
 * ImportContactsModal — CSV / XLSX upload + explicit column mapping
 * for the CRM Contacts tab.
 *
 * Two-phase UX matching the backend's two endpoints:
 *
 *   1. Upload — operator picks a file. We POST it to
 *      /api/crm/{tenantId}/contacts/import/preview, which parses the
 *      header row + the first ~10 body rows and returns them.
 *   2. Map — for each ARIA-side field (email, name, phone, ...),
 *      operator picks one of the source columns from a dropdown. The
 *      "uniformity" requirement means nothing is auto-detected; the
 *      mapping is explicit. Unmapped source columns can be rolled up
 *      into the contact's `notes` field via per-column checkboxes.
 *   3. Import — POST file + mapping (as multipart/form-data) to
 *      /api/crm/{tenantId}/contacts/import. Backend returns
 *      {imported, skipped, errors[]}. We surface that as a final
 *      summary screen the operator dismisses.
 *
 * Why two server round-trips for the file (vs. caching server-side):
 * the import endpoint stays stateless. No temp files, no per-session
 * staging table to clean up. A 1MB CSV is cheap to send twice; a
 * stateful staging design would be a maintenance burden.
 */

import React, { useState, useRef, useCallback } from "react";
import { authFetch } from "@/lib/api";
import type { ClientToastOptions } from "@/lib/use-notifications";

interface PreviewResponse {
  headers: string[];
  sample_rows: string[][];
  total_row_count: number;
  supported_fields: string[];
}

interface ImportResultRow {
  row: number;
  reason: string;
}

interface ImportResponse {
  imported: number;
  skipped: number;
  errors: ImportResultRow[];
  total_rows_in_file: number;
}

interface Props {
  open: boolean;
  onClose: () => void;
  tenantId: string;
  apiUrl: string;
  /** Called after a successful import so the parent can refetch the
   *  contacts list and show the new rows. */
  onImported: () => void;
  showToast: (opts: ClientToastOptions) => void;
}

// ARIA field → human label for the mapping form. Order is the order
// the form renders them; email + name are listed first because the
// backend rejects mappings with neither.
const FIELD_LABELS: { key: string; label: string; hint?: string }[] = [
  { key: "email", label: "Email", hint: "Primary identity. Skipped if blank." },
  { key: "name", label: "Name", hint: "Full name. Required when email is blank." },
  { key: "phone", label: "Phone" },
  { key: "title", label: "Title / Job role" },
  { key: "company", label: "Company", hint: "Auto-creates the company if it doesn't exist." },
  { key: "status", label: "Status", hint: "Coerced onto lead/qualified/customer/etc." },
  { key: "source", label: "Source" },
  { key: "tags", label: "Tags", hint: "Split on , ; or |." },
  { key: "notes", label: "Notes" },
];

const SKIP_VALUE = "__skip__";

export default function ImportContactsModal({
  open,
  onClose,
  tenantId,
  apiUrl,
  onImported,
  showToast,
}: Props) {
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<PreviewResponse | null>(null);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [extraNotesCols, setExtraNotesCols] = useState<Set<string>>(new Set());
  const [importing, setImporting] = useState(false);
  const [result, setResult] = useState<ImportResponse | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const reset = useCallback(() => {
    setFile(null);
    setPreview(null);
    setMapping({});
    setExtraNotesCols(new Set());
    setResult(null);
    setImporting(false);
    setPreviewLoading(false);
    if (fileInputRef.current) fileInputRef.current.value = "";
  }, []);

  const handleClose = () => {
    reset();
    onClose();
  };

  const handleFileChosen = async (selected: File) => {
    if (!selected) return;
    const validExt = /\.(csv|tsv|txt|xlsx|xlsm)$/i.test(selected.name);
    if (!validExt) {
      showToast({
        title: "Unsupported file type",
        body: "Please upload a .csv or .xlsx file.",
        variant: "error",
      });
      return;
    }
    if (selected.size > 25 * 1024 * 1024) {
      showToast({
        title: "File too large",
        body: "Max 25MB. Split into chunks and re-upload.",
        variant: "error",
      });
      return;
    }
    setFile(selected);
    setPreviewLoading(true);
    try {
      const fd = new FormData();
      fd.append("file", selected);
      const res = await authFetch(
        `${apiUrl}/api/crm/${tenantId}/contacts/import/preview`,
        { method: "POST", body: fd, headers: {} },
      );
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody?.detail || `preview failed (${res.status})`);
      }
      const data: PreviewResponse = await res.json();
      setPreview(data);
      // Sensible default mapping: nothing is mapped. Operator must
      // choose explicitly per the uniformity requirement.
      const empty: Record<string, string> = {};
      for (const f of data.supported_fields) empty[f] = "";
      setMapping(empty);
    } catch (err: any) {
      showToast({
        title: "Couldn't read file",
        body: err?.message || "Make sure it's a valid CSV or XLSX.",
        variant: "error",
      });
      setFile(null);
    } finally {
      setPreviewLoading(false);
    }
  };

  const handleMappingChange = (fieldKey: string, sourceCol: string) => {
    setMapping((prev) => ({
      ...prev,
      [fieldKey]: sourceCol === SKIP_VALUE ? "" : sourceCol,
    }));
  };

  const toggleExtraNotesCol = (col: string) => {
    setExtraNotesCols((prev) => {
      const next = new Set(prev);
      if (next.has(col)) next.delete(col);
      else next.add(col);
      return next;
    });
  };

  // Source columns that are NOT bound to any primary field. These are
  // the candidates for the "roll into notes" panel — keeps incidental
  // data findable.
  const unmappedSourceCols = (() => {
    if (!preview) return [];
    const used = new Set(Object.values(mapping).filter(Boolean));
    return preview.headers.filter((h) => !used.has(h));
  })();

  const canImport = !!preview && !!file && (mapping.email || mapping.name);

  const handleImport = async () => {
    if (!canImport || !file) return;
    setImporting(true);
    try {
      // Strip empty mappings so the backend doesn't see "email": ""
      // (which would just no-op anyway, but keeps the wire payload
      // tidy and the audit log readable).
      const cleanMapping: Record<string, string> = {};
      for (const [k, v] of Object.entries(mapping)) {
        if (v) cleanMapping[k] = v;
      }
      const fd = new FormData();
      fd.append("file", file);
      fd.append("mapping", JSON.stringify(cleanMapping));
      fd.append("extra_notes_columns", JSON.stringify(Array.from(extraNotesCols)));

      const res = await authFetch(
        `${apiUrl}/api/crm/${tenantId}/contacts/import`,
        { method: "POST", body: fd, headers: {} },
      );
      if (!res.ok) {
        const errBody = await res.json().catch(() => ({}));
        throw new Error(errBody?.detail || `import failed (${res.status})`);
      }
      const data: ImportResponse = await res.json();
      setResult(data);
      if (data.imported > 0) {
        onImported();
      }
    } catch (err: any) {
      showToast({
        title: "Import failed",
        body: err?.message || "Network error — please try again.",
        variant: "error",
      });
    } finally {
      setImporting(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-[90] flex items-center justify-center">
      <div className="absolute inset-0 bg-black/40" onClick={handleClose} />
      <div className="relative bg-white rounded-xl border border-[#E0DED8] shadow-2xl w-full max-w-3xl mx-4 max-h-[90vh] overflow-hidden flex flex-col">
        <div className="flex items-center justify-between px-6 py-4 border-b border-[#E0DED8] shrink-0">
          <div>
            <h3 className="text-base font-semibold text-[#2C2C2A]">Import Contacts</h3>
            <p className="text-xs text-[#9E9C95]">
              Upload a CSV or Excel file, then map each column to an ARIA field.
            </p>
          </div>
          <button onClick={handleClose} className="text-[#9E9C95] hover:text-[#2C2C2A]" aria-label="Close">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-6 space-y-5">
          {/* ── Result screen — final summary after import ── */}
          {result ? (
            <div className="space-y-4">
              <div className="bg-emerald-50 border border-emerald-200 rounded-lg p-4">
                <p className="text-sm font-semibold text-emerald-800">
                  Imported {result.imported} {result.imported === 1 ? "contact" : "contacts"}
                </p>
                <p className="text-xs text-emerald-700 mt-1">
                  Skipped {result.skipped} (already existed) · {result.errors.length} error{result.errors.length === 1 ? "" : "s"} from {result.total_rows_in_file} total row{result.total_rows_in_file === 1 ? "" : "s"}
                </p>
              </div>
              {result.errors.length > 0 && (
                <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
                  <p className="text-xs font-semibold text-amber-900 mb-2">First {Math.min(10, result.errors.length)} errors</p>
                  <ul className="space-y-1 text-xs text-amber-900 max-h-40 overflow-y-auto">
                    {result.errors.slice(0, 10).map((e) => (
                      <li key={e.row}>
                        Row {e.row}: <span className="text-amber-800">{e.reason}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              <div className="flex justify-end gap-2 pt-2">
                <button
                  onClick={() => { reset(); }}
                  className="px-4 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition"
                >
                  Import another file
                </button>
                <button
                  onClick={handleClose}
                  className="px-4 py-2 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition"
                >
                  Done
                </button>
              </div>
            </div>
          ) : (
            <>
              {/* ── Step 1: file picker ── */}
              {!preview && (
                <div>
                  <label
                    htmlFor="import-file-input"
                    className={`flex flex-col items-center justify-center gap-2 border-2 border-dashed rounded-lg py-10 px-6 cursor-pointer transition ${
                      previewLoading
                        ? "border-[#534AB7] bg-[#EEEDFE]"
                        : "border-[#E0DED8] hover:border-[#534AB7] hover:bg-[#F8F8F6]"
                    }`}
                  >
                    <svg className="w-8 h-8 text-[#9E9C95]" fill="none" stroke="currentColor" strokeWidth={1.5} viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" d="M9 8.25H7.5a2.25 2.25 0 00-2.25 2.25v9a2.25 2.25 0 002.25 2.25h9a2.25 2.25 0 002.25-2.25v-9a2.25 2.25 0 00-2.25-2.25H15M9 12l3 3m0 0l3-3m-3 3V2.25" />
                    </svg>
                    <p className="text-sm font-medium text-[#2C2C2A]">
                      {previewLoading ? "Reading file..." : "Click to upload or drop here"}
                    </p>
                    <p className="text-xs text-[#9E9C95]">CSV or Excel (.xlsx) — up to 25MB</p>
                    <input
                      ref={fileInputRef}
                      id="import-file-input"
                      type="file"
                      accept=".csv,.tsv,.txt,.xlsx,.xlsm,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv"
                      className="hidden"
                      onChange={(e) => {
                        const f = e.target.files?.[0];
                        if (f) handleFileChosen(f);
                      }}
                      disabled={previewLoading}
                    />
                  </label>
                </div>
              )}

              {/* ── Step 2: mapping form ── */}
              {preview && file && (
                <>
                  <div className="bg-[#F8F8F6] border border-[#E0DED8] rounded-lg p-3 flex items-center justify-between">
                    <div className="min-w-0">
                      <p className="text-sm font-medium text-[#2C2C2A] truncate">{file.name}</p>
                      <p className="text-xs text-[#9E9C95]">
                        {preview.total_row_count} row{preview.total_row_count === 1 ? "" : "s"} · {preview.headers.length} column{preview.headers.length === 1 ? "" : "s"}
                      </p>
                    </div>
                    <button
                      onClick={() => { setFile(null); setPreview(null); setMapping({}); setExtraNotesCols(new Set()); }}
                      className="text-xs text-[#534AB7] hover:underline shrink-0 ml-3"
                    >
                      Replace file
                    </button>
                  </div>

                  <div>
                    <h4 className="text-sm font-semibold text-[#2C2C2A] mb-2">Map columns</h4>
                    <p className="text-xs text-[#5F5E5A] mb-3">
                      Pick which source column to use for each ARIA field. At minimum, map either Email or Name — rows without either are skipped.
                    </p>
                    <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                      {FIELD_LABELS.map((f) => (
                        <div key={f.key} className="space-y-1">
                          <label className="text-xs font-semibold text-[#5F5E5A] uppercase">
                            {f.label}
                            {(f.key === "email" || f.key === "name") && (
                              <span className="text-[#534AB7] ml-1">*</span>
                            )}
                          </label>
                          <select
                            value={mapping[f.key] || SKIP_VALUE}
                            onChange={(e) => handleMappingChange(f.key, e.target.value)}
                            className="w-full text-sm bg-white border border-[#E0DED8] rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30"
                          >
                            <option value={SKIP_VALUE}>— skip this field —</option>
                            {preview.headers.map((h) => (
                              <option key={h} value={h}>{h}</option>
                            ))}
                          </select>
                          {f.hint && (
                            <p className="text-[10px] text-[#9E9C95]">{f.hint}</p>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>

                  {unmappedSourceCols.length > 0 && (
                    <div>
                      <h4 className="text-sm font-semibold text-[#2C2C2A] mb-1">Unmapped columns</h4>
                      <p className="text-xs text-[#5F5E5A] mb-3">
                        Tick any to roll into the contact's <code>notes</code> field as <code>Header: Value</code>. Otherwise they're dropped.
                      </p>
                      <div className="flex flex-wrap gap-2">
                        {unmappedSourceCols.map((col) => {
                          const checked = extraNotesCols.has(col);
                          return (
                            <button
                              key={col}
                              onClick={() => toggleExtraNotesCol(col)}
                              className={`px-3 py-1.5 text-xs rounded-full border transition ${
                                checked
                                  ? "bg-[#534AB7] text-white border-[#534AB7]"
                                  : "bg-white text-[#5F5E5A] border-[#E0DED8] hover:border-[#534AB7]"
                              }`}
                            >
                              {checked ? "✓ " : "+ "}{col}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                  )}

                  <div>
                    <h4 className="text-sm font-semibold text-[#2C2C2A] mb-2">Sample (first {preview.sample_rows.length} rows)</h4>
                    <div className="border border-[#E0DED8] rounded-lg overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead className="bg-[#F8F8F6]">
                          <tr>
                            {preview.headers.map((h) => (
                              <th key={h} className="text-left font-semibold text-[#5F5E5A] px-2 py-1.5 border-b border-[#E0DED8] whitespace-nowrap">
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {preview.sample_rows.map((row, i) => (
                            <tr key={i} className="hover:bg-[#F8F8F6]">
                              {preview.headers.map((_, j) => (
                                <td key={j} className="px-2 py-1.5 border-b border-[#E0DED8] text-[#2C2C2A] whitespace-nowrap max-w-[200px] overflow-hidden text-ellipsis">
                                  {row[j] || ""}
                                </td>
                              ))}
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </div>
                </>
              )}
            </>
          )}
        </div>

        {/* Footer — only render when we have a file ready to import */}
        {preview && !result && (
          <div className="border-t border-[#E0DED8] px-6 py-3 flex items-center justify-end gap-2 shrink-0 bg-[#F8F8F6]">
            <button
              onClick={handleClose}
              disabled={importing}
              className="px-4 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-white transition disabled:opacity-60"
            >
              Cancel
            </button>
            <button
              onClick={handleImport}
              disabled={!canImport || importing}
              title={!canImport ? "Map either Email or Name first" : ""}
              className="px-4 py-2 text-sm font-semibold rounded-lg bg-[#534AB7] text-white hover:bg-[#433AA0] transition disabled:opacity-50 flex items-center gap-2"
            >
              {importing && (
                <div className="w-3.5 h-3.5 border-2 border-white border-t-transparent rounded-full animate-spin" />
              )}
              {importing ? "Importing..." : `Import ${preview.total_row_count} row${preview.total_row_count === 1 ? "" : "s"}`}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
