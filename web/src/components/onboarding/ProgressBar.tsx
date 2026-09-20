import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

interface ProgressBarProps {
  currentStep: number; // 0-indexed
  totalSteps: number;
  estimatedMinutes: number;
}

export function ProgressBar({
  currentStep,
  totalSteps,
  estimatedMinutes,
}: ProgressBarProps) {
  return (
    <div className="flex flex-col items-center gap-3">
      {/* Step circles with connecting lines */}
      <div className="flex items-center">
        {Array.from({ length: totalSteps }, (_, i) => (
          <div key={i} className="flex items-center">
            {/* Step circle */}
            <div
              className={cn(
                "flex h-10 w-10 items-center justify-center rounded-full text-sm font-medium transition-all duration-300",
                i < currentStep &&
                  "bg-primary text-primary-foreground shadow-warm-sm",
                i === currentStep &&
                  "border-2 border-amber-500 text-amber-600 ring-4 ring-amber-100 bg-amber-50",
                i > currentStep &&
                  "border-2 border-muted text-muted-foreground bg-background"
              )}
            >
              {i < currentStep ? <Check className="h-4 w-4" /> : i + 1}
            </div>

            {/* Connecting line */}
            {i < totalSteps - 1 && (
              <div
                className={cn(
                  "mx-2 h-0.5 w-10 transition-colors duration-300 sm:w-16 rounded-full",
                  i < currentStep ? "bg-primary" : "bg-muted"
                )}
              />
            )}
          </div>
        ))}
      </div>

      {/* Subtext */}
      <p className="text-sm text-muted-foreground">
        Step <span className="font-mono font-medium">{currentStep + 1}</span> of{" "}
        <span className="font-mono font-medium">{totalSteps}</span> — About{" "}
        <span className="font-mono">{estimatedMinutes}</span>{" "}
        {estimatedMinutes === 1 ? "minute" : "minutes"}
      </p>
    </div>
  );
}
