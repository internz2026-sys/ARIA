"use client";

import React from "react";

interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description: string;
}

export default function EmptyState({ icon, title, description }: EmptyStateProps) {
  return (
    <div className="bg-white rounded-xl border border-[#E0DED8] p-12 text-center">
      {icon && (
        <div className="w-12 h-12 rounded-full bg-[#EEEDFE] flex items-center justify-center mx-auto mb-4">
          {icon}
        </div>
      )}
      <p className="text-[#2C2C2A] font-semibold mb-1">{title}</p>
      <p className="text-sm text-[#5F5E5A]">{description}</p>
    </div>
  );
}
