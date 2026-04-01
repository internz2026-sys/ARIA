"use client";

import React from "react";

export interface ConfirmationData {
  title: string;
  message: string;
  action: string;
  params: Record<string, any>;
  confirm_label: string;
  cancel_label: string;
  destructive: boolean;
}

interface ConfirmationDialogProps {
  data: ConfirmationData;
  onConfirm: () => void;
  onCancel: () => void;
  loading?: boolean;
}

export default function ConfirmationDialog({ data, onConfirm, onCancel, loading }: ConfirmationDialogProps) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 backdrop-blur-sm">
      <div className="bg-white rounded-xl border border-[#E0DED8] shadow-xl max-w-md w-full mx-4 overflow-hidden">
        {/* Header */}
        <div className="px-6 pt-6 pb-2">
          <div className="flex items-center gap-2">
            {data.destructive ? (
              <div className="w-10 h-10 rounded-full bg-red-50 flex items-center justify-center flex-shrink-0">
                <svg className="w-5 h-5 text-red-500" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
                </svg>
              </div>
            ) : (
              <div className="w-10 h-10 rounded-full bg-[#EEEDFE] flex items-center justify-center flex-shrink-0">
                <svg className="w-5 h-5 text-[#534AB7]" fill="none" stroke="currentColor" strokeWidth={2} viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
            )}
            <h3 className="text-lg font-semibold text-[#2C2C2A]">{data.title}</h3>
          </div>
        </div>

        {/* Body */}
        <div className="px-6 py-4">
          <p className="text-sm text-[#5F5E5A] leading-relaxed">{data.message}</p>

          {/* Show params for transparency */}
          {Object.keys(data.params).length > 0 && (
            <div className="mt-3 bg-[#F8F8F6] rounded-lg p-3">
              <p className="text-xs font-medium text-[#5F5E5A] mb-1">Details:</p>
              {Object.entries(data.params).map(([key, value]) => (
                <div key={key} className="flex items-center gap-2 text-xs text-[#2C2C2A]">
                  <span className="text-[#9E9C95]">{key}:</span>
                  <span className="font-medium">{typeof value === "object" ? JSON.stringify(value) : String(value)}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Actions */}
        <div className="px-6 pb-6 flex items-center justify-end gap-3">
          <button
            onClick={onCancel}
            disabled={loading}
            className="px-4 py-2 text-sm font-medium rounded-lg border border-[#E0DED8] text-[#5F5E5A] hover:bg-[#F8F8F6] transition-colors disabled:opacity-50"
          >
            {data.cancel_label}
          </button>
          <button
            onClick={onConfirm}
            disabled={loading}
            className={`px-4 py-2 text-sm font-medium rounded-lg text-white transition-colors disabled:opacity-50 ${
              data.destructive
                ? "bg-red-500 hover:bg-red-600"
                : "bg-[#534AB7] hover:bg-[#433AA0]"
            }`}
          >
            {loading ? "Processing..." : data.confirm_label}
          </button>
        </div>
      </div>
    </div>
  );
}
