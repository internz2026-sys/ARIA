"use client";

import React, { useState, useCallback } from "react";
import { useEditor, EditorContent } from "@tiptap/react";
import StarterKit from "@tiptap/starter-kit";
import Link from "@tiptap/extension-link";
import Underline from "@tiptap/extension-underline";

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

function ToolbarButton({
  active,
  onClick,
  title,
  children,
}: {
  active?: boolean;
  onClick: () => void;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={`p-1.5 rounded transition-colors ${
        active ? "bg-[#EEEDFE] text-[#534AB7]" : "text-[#5F5E5A] hover:bg-[#F8F8F6] hover:text-[#2C2C2A]"
      }`}
    >
      {children}
    </button>
  );
}

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

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3] },
      }),
      Link.configure({ openOnClick: false }),
      Underline,
    ],
    content: htmlBody,
    editorProps: {
      attributes: {
        class: "prose prose-sm max-w-none focus:outline-none min-h-[200px] px-4 py-3 text-[#2C2C2A]",
      },
    },
  });

  const handleSave = useCallback(async () => {
    if (!editor) return;
    setSaving(true);
    try {
      await onSave({ to, subject, html_body: editor.getHTML() });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch {}
    setSaving(false);
  }, [editor, to, subject, onSave]);

  const handleSend = useCallback(async () => {
    if (!editor) return;
    // Auto-save before sending
    setSaving(true);
    try {
      await onSave({ to, subject, html_body: editor.getHTML() });
    } catch {}
    setSaving(false);
    onSend();
  }, [editor, to, subject, onSave, onSend]);

  if (!editor) return null;

  return (
    <div className="flex flex-col w-full">
      {/* Envelope fields */}
      <div className="border-b border-[#E0DED8] p-5 space-y-3">
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

      {/* Action bar — above editor for quick access */}
      <div className="border-b border-[#E0DED8] px-5 py-3 flex items-center gap-2 bg-[#F8F8F6]">
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

      {/* Toolbar */}
      <div className="border-b border-[#E0DED8] px-5 py-1.5 flex items-center gap-0.5 flex-wrap">
        <ToolbarButton active={editor.isActive("bold")} onClick={() => editor.chain().focus().toggleBold().run()} title="Bold">
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M15.6 10.79c.97-.67 1.65-1.77 1.65-2.79 0-2.26-1.75-4-4-4H7v14h7.04c2.09 0 3.71-1.7 3.71-3.79 0-1.52-.86-2.82-2.15-3.42zM10 6.5h3c.83 0 1.5.67 1.5 1.5s-.67 1.5-1.5 1.5h-3v-3zm3.5 9H10v-3h3.5c.83 0 1.5.67 1.5 1.5s-.67 1.5-1.5 1.5z"/></svg>
        </ToolbarButton>
        <ToolbarButton active={editor.isActive("italic")} onClick={() => editor.chain().focus().toggleItalic().run()} title="Italic">
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M10 4v3h2.21l-3.42 8H6v3h8v-3h-2.21l3.42-8H18V4z"/></svg>
        </ToolbarButton>
        <ToolbarButton active={editor.isActive("underline")} onClick={() => editor.chain().focus().toggleUnderline().run()} title="Underline">
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M12 17c3.31 0 6-2.69 6-6V3h-2.5v8c0 1.93-1.57 3.5-3.5 3.5S8.5 12.93 8.5 11V3H6v8c0 3.31 2.69 6 6 6zm-7 2v2h14v-2H5z"/></svg>
        </ToolbarButton>

        <div className="w-px h-5 bg-[#E0DED8] mx-1" />

        <ToolbarButton active={editor.isActive("bulletList")} onClick={() => editor.chain().focus().toggleBulletList().run()} title="Bullet list">
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M4 10.5c-.83 0-1.5.67-1.5 1.5s.67 1.5 1.5 1.5 1.5-.67 1.5-1.5-.67-1.5-1.5-1.5zm0-6c-.83 0-1.5.67-1.5 1.5S3.17 7.5 4 7.5 5.5 6.83 5.5 6 4.83 4.5 4 4.5zm0 12c-.83 0-1.5.68-1.5 1.5s.68 1.5 1.5 1.5 1.5-.68 1.5-1.5-.67-1.5-1.5-1.5zM7 19h14v-2H7v2zm0-6h14v-2H7v2zm0-8v2h14V5H7z"/></svg>
        </ToolbarButton>
        <ToolbarButton active={editor.isActive("orderedList")} onClick={() => editor.chain().focus().toggleOrderedList().run()} title="Numbered list">
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M2 17h2v.5H3v1h1v.5H2v1h3v-4H2v1zm1-9h1V4H2v1h1v3zm-1 3h1.8L2 13.1v.9h3v-1H3.2L5 10.9V10H2v1zm5-6v2h14V5H7zm0 14h14v-2H7v2zm0-6h14v-2H7v2z"/></svg>
        </ToolbarButton>

        <div className="w-px h-5 bg-[#E0DED8] mx-1" />

        <ToolbarButton
          active={editor.isActive("link")}
          onClick={() => {
            if (editor.isActive("link")) {
              editor.chain().focus().unsetLink().run();
            } else {
              const url = prompt("Enter URL:");
              if (url) editor.chain().focus().setLink({ href: url }).run();
            }
          }}
          title="Link"
        >
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z"/></svg>
        </ToolbarButton>

        <ToolbarButton onClick={() => editor.chain().focus().setHorizontalRule().run()} title="Divider">
          <svg className="w-4 h-4" viewBox="0 0 24 24" fill="currentColor"><path d="M4 11h16v2H4z"/></svg>
        </ToolbarButton>
      </div>

      {/* Editor body */}
      <div className="flex-1 overflow-auto bg-white">
        <EditorContent editor={editor} />
      </div>

    </div>
  );
}
