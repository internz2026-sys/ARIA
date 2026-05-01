"use client";

import React, { Suspense } from "react";
import { useSearchParams } from "next/navigation";

export default function CheckEmailPage() {
  return (
    <Suspense
      fallback={
        <div className="w-full max-w-[420px] mx-auto px-6 py-10">
          <div className="h-72 flex items-center justify-center">
            <div className="w-8 h-8 border-2 border-[#534AB7] border-t-transparent rounded-full animate-spin" />
          </div>
        </div>
      }
    >
      <CheckEmailContent />
    </Suspense>
  );
}

function CheckEmailContent() {
  const searchParams = useSearchParams();
  const email = searchParams.get("email") || "";

  return (
    <div className="w-full max-w-[420px] mx-auto px-6 py-10">
      <div className="bg-white rounded-2xl border border-[#E0DED8] shadow-sm p-6 sm:p-8">
        <div className="flex justify-center mb-5">
          <div className="w-14 h-14 rounded-full bg-[#EEEDFE] flex items-center justify-center">
            <svg width="26" height="26" fill="none" viewBox="0 0 24 24">
              <path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" stroke="#534AB7" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              <polyline points="22,6 12,13 2,6" stroke="#534AB7" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
        </div>

        <h1 className="text-[24px] font-bold text-[#2C2C2A] text-center mb-2">Check your email</h1>
        <p className="text-[#5F5E5A] text-center text-[15px] mb-2">
          We sent a verification link to
        </p>
        {email && (
          <p className="text-[#2C2C2A] text-center text-[15px] font-medium mb-6 break-all">
            {email}
          </p>
        )}
        <p className="text-[#5F5E5A] text-center text-[14px] mb-6">
          Click the link in your inbox to activate your account. You can close this tab.
        </p>

        <div className="rounded-lg bg-[#F8F8F6] border border-[#E0DED8] p-3 text-xs text-[#5F5E5A] mb-6">
          <p>
            <span className="font-medium text-[#2C2C2A]">Didn&apos;t get it?</span>{" "}
            Check your spam folder. If still nothing, you can{" "}
            <a href="/signup" className="text-[#534AB7] hover:underline">try again</a>.
          </p>
        </div>

        <a
          href="/login"
          className="flex items-center justify-center gap-1.5 text-sm text-[#534AB7] font-medium hover:underline"
        >
          <svg width="16" height="16" fill="none" viewBox="0 0 24 24"><path d="M19 12H5M12 19l-7-7 7-7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
          Back to sign in
        </a>
      </div>
    </div>
  );
}
