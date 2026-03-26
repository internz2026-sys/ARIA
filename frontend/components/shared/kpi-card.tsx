import { cn } from "@/lib/utils";
import { TrendingUp, TrendingDown, Minus } from "lucide-react";

interface KpiCardProps {
  label: string;
  value: string;
  change?: string;
  trend?: "up" | "down" | "neutral";
  className?: string;
}

export default function KpiCard({ label, value, change, trend = "neutral", className }: KpiCardProps) {
  const trendColor = trend === "up" ? "text-success" : trend === "down" ? "text-danger" : "text-text-secondary";
  const TrendIcon = trend === "up" ? TrendingUp : trend === "down" ? TrendingDown : Minus;

  return (
    <div className={cn("bg-white rounded-card border border-border p-5 hover:shadow-sm transition-shadow", className)}>
      <p className="text-sm text-text-secondary font-medium">{label}</p>
      <p className="text-3xl font-semibold text-text-primary mt-1">{value}</p>
      {change && (
        <div className={cn("flex items-center gap-1.5 mt-2", trendColor)}>
          <TrendIcon className="h-4 w-4" />
          <span className="text-xs font-medium">{change}</span>
        </div>
      )}
    </div>
  );
}
