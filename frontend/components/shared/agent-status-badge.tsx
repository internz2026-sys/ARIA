import { cn } from "@/lib/utils";

type AgentStatus = "running" | "idle" | "paused" | "error" | "waiting";

interface AgentStatusBadgeProps {
  status: AgentStatus;
  className?: string;
}

const statusConfig: Record<AgentStatus, { label: string; color: string; bg: string; pulse: boolean }> = {
  running: { label: "Running", color: "text-success", bg: "bg-success/10", pulse: true },
  idle: { label: "Idle", color: "text-text-secondary", bg: "bg-bg-secondary", pulse: false },
  paused: { label: "Paused", color: "text-warning", bg: "bg-warning/10", pulse: false },
  error: { label: "Error", color: "text-danger", bg: "bg-danger/10", pulse: false },
  waiting: { label: "Waiting", color: "text-warning", bg: "bg-warning/10", pulse: false },
};

export default function AgentStatusBadge({ status, className }: AgentStatusBadgeProps) {
  const config = statusConfig[status];

  return (
    <span className={cn("inline-flex items-center gap-1.5 text-xs font-medium px-2 py-0.5 rounded-full", config.bg, config.color, className)}>
      <span className="relative flex h-2 w-2">
        {config.pulse && (
          <span className={cn("animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 bg-success")} />
        )}
        <span className={cn("relative inline-flex rounded-full h-2 w-2", status === "running" ? "bg-success" : status === "idle" ? "bg-text-secondary" : status === "paused" || status === "waiting" ? "bg-warning" : "bg-danger")} />
      </span>
      {config.label}
    </span>
  );
}
