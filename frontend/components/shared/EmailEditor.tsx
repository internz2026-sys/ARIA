"use client";

import React, { useState, useCallback, useRef, useEffect } from "react";

interface EmailEditorProps {
  to: string;
  subject: string;
  htmlBody: string;
  onSave: (data: { to: string; subject: string; html_body: string }) => Promise<void>;
  onSend: () => void;
  onCancel: () => void;
  sendDisabled?: boolean;
  sendLoading?: boolean;
  cancelLoading?: boolean;
}

type EditorTab = "preview" | "edit" | "source";

export default function EmailEditor({
  to: initialTo,
  subject: initialSubject,
  htmlBody,
  onSave,
  onSend,
  onCancel,
  sendDisabled,
  sendLoading,
  cancelLoading,
}: EmailEditorProps) {
  const [to, setTo] = useState(initialTo);
  const [subject, setSubject] = useState(initialSubject);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [tab, setTab] = useState<EditorTab>("edit");
  const [sourceHtml, setSourceHtml] = useState(htmlBody);
  const editIframeRef = useRef<HTMLIFrameElement>(null);
  const previewIframeRef = useRef<HTMLIFrameElement>(null);

  // Set up contentEditable on the edit iframe
  useEffect(() => {
    const iframe = editIframeRef.current;
    if (!iframe || tab !== "edit") return;

    const onLoad = () => {
      const doc = iframe.contentDocument;
      if (!doc?.body) return;
      doc.body.contentEditable = "true";
      doc.body.style.outline = "none";
      doc.body.style.cursor = "text";
      // Auto-resize
      const resize = () => {
        iframe.style.height = `${Math.max(doc.body.scrollHeight + 20, 300)}px`;
      };
      resize();
      const observer = new MutationObserver(resize);
      observer.observe(doc.body, { childList: true, subtree: true, characterData: true });
      doc.body.addEventListener("input", resize);
    };

    iframe.addEventListener("load", onLoad);
    // If already loaded (re-render), trigger immediately
    if (iframe.contentDocument?.body) onLoad();
    return () => iframe.removeEventListener("load", onLoad);
  }, [tab]);

  // Auto-resize preview iframe
  useEffect(() => {
    const iframe = previewIframeRef.current;
    if (!iframe || tab !== "preview") return;
    const onLoad = () => {
      const doc = iframe.contentDocument;
      if (doc?.body) {
        iframe.style.height = `${Math.max(doc.body.scrollHeight + 20, 300)}px`;
      }
    };
    iframe.addEventListener("load", onLoad);
    return () => iframe.removeEventListener("load", onLoad);
  }, [tab]);

  /** Get the current HTML from whichever mode is active */
  const getCurrentHtml = useCallback((): string => {
    if (tab === "source") return sourceHtml;
    if (tab === "edit") {
      const doc = editIframeRef.current?.contentDocument;
      if (doc) return "<!DOCTYPE html>" + doc.documentElement.outerHTML;
    }
    return sourceHtml;
  }, [tab, sourceHtml]);

  // Sync HTML between tabs when switching
  const handleTabSwitch = (newTab: EditorTab) => {
    // Save current state before switching
    const current = getCurrentHtml();
    setSourceHtml(current);
    setTab(newTab);
  };

  const handleSave = useCallback(async () => {
    setSaving(true);
    try {
      await onSave({ to, subject, html_body: getCurrentHtml() });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {}
    setSaving(false);
  }, [to, subject, onSave, getCurrentHtml]);

  const handleSend = useCallback(async () => {
    setSaving(true);
    try {
      await onSave({ to, subject, html_body: getCurrentHtml() });
    } catch {}
    setSaving(false);
    onSend();
  }, [to, subject, onSave, onSend, getCurrentHtml]);

  const tabs: { key: EditorTab; label: string; desc: string }[] = [
    { key: "edit", label: "Edit", desc: "Click text to edit" },
    { key: "preview", label: "Preview", desc: "How it looks when sent" },
    { key: "source", label: "Source", desc: "Edit raw HTML" },
  ];

  return (
    <div className="flex flex-col w-full h-full">
      {/* Envelope fields */}
      <div className="border-b border-[#E0DED8] p-5 space-y-3 shrink-0">
        <div className="flex items-center gap-2">
          <label className="text-xs font-semibold text-[#5F5E5A] uppercase w-16 shrink-0">To</label>
          <input
            type="email"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            placeholder="recipient@example.com"
            className="flex-1 text-sm text-[#2C2C2A] bg-[#F8F8F6] border border-[#E0DED8] rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7]"
          />
        </div>
        <div className="flex items-center gap-2">
          <label className="text-xs font-semibold text-[#5F5E5A] uppercase w-16 shrink-0">Subject</label>
          <input
            type="text"
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
            placeholder="Email subject"
            className="flex-1 text-sm font-medium text-[#2C2C2A] bg-[#F8F8F6] border border-[#E0DED8] rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-[#534AB7]/30 focus:border-[#534AB7]"
          />
        </div>
      </div>

      {/* Action bar */}
      <div className="border-b border-[#E0DED8] px-5 py-3 flex items-center gap-2 bg-[#F8F8F6] shrink-0">
        <button
          onClick={handleSend}
          disabled={sendDisabled || sendLoading || !to}
          title={!to ? "Add a recipient first" : ""}
          className="flex items-center gap-1.5 px-4 py-2 text-sm font-semibold rounded-lg bg-[#1D9E75] text-white hover:bg-[#178a64] transition-colors disabled:opacity-60"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" d="M6 12L3.269 3.126A59.768 59.768 0 0121.485 12 59.77 59.77 0 013.27 20.876L5.999 12zm0 0h7.5" />
          </svg>
          {sendLoading ? "Sending..." : "Approve & Send"}
        </button>
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-1.5 px-3 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] bg-white text-[#5F5E5A] hover:bg-[#F0EFEC] transition-colors disabled:opacity-60"
        >
          {saving ? "Saving..." : saved ? "Saved!" : "Save changes"}
        </button>
        <button
          onClick={onCancel}
          disabled={cancelLoading}
          className="ml-auto px-3 py-2 text-sm font-medium rounded-lg border border-red-200 text-red-500 hover:bg-red-50 transition-colors disabled:opacity-60"
        >
          {cancelLoading ? "Cancelling..." : "Cancel draft"}
        </button>
      </div>

      {/* Tabs: Edit / Preview / Source */}
      <div className="border-b border-[#E0DED8] px-5 flex items-center gap-1 shrink-0">
        {tabs.map((t) => (
          <button
            key={t.key}
            onClick={() => handleTabSwitch(t.key)}
            className={`px-3 py-2.5 text-sm font-medium border-b-2 transition-colors ${
              tab === t.key
                ? "border-[#534AB7] text-[#534AB7]"
                : "border-transparent text-[#5F5E5A] hover:text-[#2C2C2A]"
            }`}
            title={t.desc}
          >
            {t.label}
          </button>
        ))}
        <span className="ml-auto text-[10px] text-[#9E9C95]">
          {tabs.find((t) => t.key === tab)?.desc}
        </span>
      </div>

      {/* Content area */}
      <div className="flex-1 overflow-auto bg-white">
        {/* Edit mode: contentEditable iframe — preserves all HTML/CSS styling */}
        {tab === "edit" && (
          <iframe
            ref={editIframeRef}
            key={"edit-" + htmlBody.slice(0, 50)}
            srcDoc={sourceHtml}
            title="Edit email"
            className="w-full min-h-[300px] border-0"
            sandbox="allow-same-origin"
          />
        )}

        {/* Preview mode: read-only render */}
        {tab === "preview" && (
          <iframe
            ref={previewIframeRef}
            srcDoc={getCurrentHtml()}
            title="Email preview"
            className="w-full min-h-[300px] border-0"
            sandbox="allow-same-origin"
          />
        )}

        {/* Source mode: raw HTML editing with monospace font */}
        {tab === "source" && (
          <textarea
            value={sourceHtml}
            onChange={(e) => setSourceHtml(e.target.value)}
            spellCheck={false}
            className="w-full h-full min-h-[400px] p-4 font-mono text-xs text-[#2C2C2A] bg-[#1e1e1e] text-[#d4d4d4] leading-relaxed resize-none focus:outline-none"
            style={{ color: "#d4d4d4", backgroundColor: "#1e1e1e", tabSize: 2 }}
          />
        )}
      </div>
    </div>
  );
}
