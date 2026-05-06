"use client";

import React, { useState, useEffect } from "react";

interface ChecklistItem {
  label: string;
  done: boolean;
}

const allItems: string[] = [
  "Business profile saved",
  "Lead gen agent configured",
  "Outreach agent ready",
  "Support agent activated",
  "CRM agent connected",
  "Invoice agent deployed",
  "Social media agent online",
  "Scheduling agent synced",
  "Analytics dashboard created",
  "Notification preferences set",
];

export default function LaunchingPage() {
  const [items, setItems] = useState<ChecklistItem[]>(
    allItems.map((label) => ({ label, done: false }))
  );
  const [progress, setProgress] = useState(0);
  const [complete, setComplete] = useState(false);

  useEffect(() => {
    let current = 0;
    const interval = setInterval(() => {
      if (current < allItems.length) {
        setItems((prev) =>
          prev.map((item, i) => (i === current ? { ...item, done: true } : item))
        );
        current++;
        setProgress((current / allItems.length) * 100);
      } else {
        clearInterval(interval);
        setTimeout(() => setComplete(true), 600);
      }
    }, 700);

    return () => clearInterval(interval);
  }, []);

  return (
    <div className="min-h-[calc(100vh-73px)] flex items-center justify-center px-6 relative overflow-hidden">
      {/* Confetti CSS animation when complete */}
      {complete && (
        <div className="fixed inset-0 pointer-events-none z-50">
          {Array.from({ length: 60 }).map((_, i) => (
            <div
              key={i}
              className="absolute animate-confetti"
              style={{
                left: `${Math.random() * 100}%`,
                top: `-5%`,
                width: `${6 + Math.random() * 8}px`,
                height: `${6 + Math.random() * 8}px`,
                backgroundColor: ["#534AB7", "#1D9E75", "#BA7517", "#D85A30", "#7B6FE0", "#EEEDFE"][
                  Math.floor(Math.random() * 6)
                ],
                borderRadius: Math.random() > 0.5 ? "50%" : "2px",
                animationDelay: `${Math.random() * 2}s`,
                animationDuration: `${2 + Math.random() * 3}s`,
              }}
            />
          ))}
          <style>{`
            @keyframes confetti-fall {
              0% { transform: translateY(0) rotate(0deg); opacity: 1; }
              100% { transform: translateY(100vh) rotate(720deg); opacity: 0; }
            }
            .animate-confetti {
              animation: confetti-fall 3s ease-out forwards;
            }
          `}</style>
        </div>
      )}

      <div className="w-full max-w-lg text-center">
        {!complete ? (
          <>
            {/* Spinner */}
            <div className="w-16 h-16 mx-auto mb-8 relative">
              <div className="absolute inset-0 rounded-full border-4 border-[#E0DED8]" />
              <div
                className="absolute inset-0 rounded-full border-4 border-[#534AB7] border-t-transparent animate-spin"
              />
              <div className="absolute inset-0 flex items-center justify-center">
                <img src="/logo.png" alt="ARIA" className="h-8 w-8 rounded-full object-cover" />
              </div>
            </div>

            <h1 className="text-[28px] font-bold text-[#2C2C2A] mb-2">
              ARIA is setting up your agents...
            </h1>
            <p className="text-[#5F5E5A] text-[15px] mb-8">This will only take a moment</p>

            {/* Progress bar */}
            <div className="w-full max-w-sm mx-auto h-2 bg-[#E0DED8] rounded-full mb-8 overflow-hidden">
              <div
                className="h-full bg-[#534AB7] rounded-full transition-all duration-500 ease-out"
                style={{ width: `${progress}%` }}
              />
            </div>

            {/* Checklist */}
            <div className="space-y-2.5 text-left max-w-sm mx-auto">
              {items.map((item, i) => (
                <div
                  key={i}
                  className={`flex items-center gap-3 transition-all duration-300 ${
                    item.done ? "opacity-100" : "opacity-30"
                  }`}
                >
                  {item.done ? (
                    <div className="w-5 h-5 rounded-full bg-[#E6F7F0] flex items-center justify-center flex-shrink-0">
                      <svg width="12" height="12" fill="none" viewBox="0 0 24 24">
                        <path d="M20 6L9 17l-5-5" stroke="#1D9E75" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round"/>
                      </svg>
                    </div>
                  ) : (
                    <div className="w-5 h-5 rounded-full border-2 border-[#E0DED8] flex-shrink-0" />
                  )}
                  <span className={`text-sm ${item.done ? "text-[#2C2C2A]" : "text-[#B0AFA8]"}`}>
                    {item.label}
                  </span>
                </div>
              ))}
            </div>
          </>
        ) : (
          /* Completion state */
          <>
            <div className="w-20 h-20 rounded-full bg-[#E6F7F0] flex items-center justify-center mx-auto mb-6">
              <svg width="36" height="36" fill="none" viewBox="0 0 24 24">
                <path d="M20 6L9 17l-5-5" stroke="#1D9E75" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              </svg>
            </div>

            <h1 className="text-[32px] font-bold text-[#2C2C2A] mb-3">You&apos;re all set!</h1>
            <p className="text-[#5F5E5A] text-lg mb-8 max-w-md mx-auto">
              Your AI agents are configured and ready to go. Welcome to the future of your business.
            </p>

            <a
              href="/dashboard"
              className="inline-flex items-center gap-2 h-12 px-10 rounded-lg bg-[#534AB7] text-white font-semibold text-[15px] hover:bg-[#433AA0] transition shadow-lg shadow-[#534AB7]/20"
            >
              Go to Dashboard
              <svg width="18" height="18" fill="none" viewBox="0 0 24 24"><path d="M5 12h14M12 5l7 7-7 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/></svg>
            </a>

            <p className="text-sm text-[#5F5E5A] mt-6">
              Redirecting to dashboard in a few seconds...
            </p>
          </>
        )}
      </div>
    </div>
  );
}
