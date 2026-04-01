"use client";

import { useState, useEffect } from "react";

/** Shared hook for reading tenant_id from localStorage. */
export function useTenantId(): string {
  const [tenantId, setTenantId] = useState("");

  useEffect(() => {
    setTenantId(localStorage.getItem("aria_tenant_id") || "");
  }, []);

  return tenantId;
}

/** Non-hook version for use outside React components. */
export function getTenantId(): string {
  return (typeof window !== "undefined" && localStorage.getItem("aria_tenant_id")) || "";
}
