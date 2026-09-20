import React from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";

interface ContextualPromptProps {
  icon: React.ComponentType<{ className?: string }>;
  message: React.ReactNode;
  cta?: { label: string; to: string } | { label: string; onClick: () => void };
  variant: "primary" | "amber" | "sage";
}

const variantStyles = {
  primary: "border-primary/20 bg-primary/[0.03]",
  amber: "border-amber-200 bg-amber-50",
  sage: "border-success/20 bg-success/5",
};

export function ContextualPrompt({
  icon: Icon,
  message,
  cta,
  variant,
}: ContextualPromptProps) {
  const navigate = useNavigate();

  return (
    <div
      className={`flex items-center gap-3 rounded-xl border p-4 ${variantStyles[variant]}`}
    >
      <Icon className="h-5 w-5 flex-shrink-0 text-muted-foreground" />
      <p className="flex-1 text-sm text-muted-foreground">{message}</p>
      {cta && (
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            if ("to" in cta) {
              navigate(cta.to);
            } else {
              cta.onClick();
            }
          }}
        >
          {cta.label}
        </Button>
      )}
    </div>
  );
}
