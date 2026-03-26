import { cn } from "@/lib/utils";
import { forwardRef } from "react";

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label?: string;
  error?: string;
  helperText?: string;
}

const Input = forwardRef<HTMLInputElement, InputProps>(({ className, label, error, helperText, ...props }, ref) => (
  <div className="space-y-1">
    {label && <label className="text-sm font-medium text-text-primary">{label}</label>}
    <input
      className={cn(
        "flex h-10 w-full rounded-input border border-border bg-white px-3 py-2 text-sm placeholder:text-text-secondary/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary disabled:cursor-not-allowed disabled:opacity-50",
        error && "border-danger focus:ring-danger/20",
        className
      )}
      ref={ref}
      {...props}
    />
    {error && <p className="text-xs text-danger">{error}</p>}
    {helperText && !error && <p className="text-xs text-text-secondary">{helperText}</p>}
  </div>
));
Input.displayName = "Input";

export { Input };
