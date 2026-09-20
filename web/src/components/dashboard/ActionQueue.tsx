import { useNavigate } from "react-router-dom";
import { Button } from "@/components/ui/button";
import {
  ClipboardCheck,
  Receipt,
  AlertTriangle,
  Wifi,
  ArrowRight,
  Clock,
} from "lucide-react";

interface ActionItem {
  type: string;
  count: number;
  message: string;
  action: string;
}

interface ActionQueueProps {
  items: ActionItem[];
}

function getActionConfig(type: string) {
  switch (type) {
    case "pending_reviews":
      return {
        icon: ClipboardCheck,
        bg: "bg-indigo-50",
        border: "border-primary/15",
        iconColor: "text-primary",
        iconBg: "bg-primary/10",
        cta: "Review Now",
        estimate: (count: number) => `~${Math.max(1, Math.ceil(count * 0.4))} min`,
      };
    case "missing_receipts":
      return {
        icon: Receipt,
        bg: "bg-amber-50",
        border: "border-amber-200",
        iconColor: "text-amber-600",
        iconBg: "bg-amber-100",
        cta: "Upload",
        estimate: () => null,
      };
    case "wise_detected":
      return {
        icon: Wifi,
        bg: "bg-indigo-50",
        border: "border-primary/15",
        iconColor: "text-primary",
        iconBg: "bg-primary/10",
        cta: "Connect",
        estimate: () => null,
      };
    default:
      return {
        icon: AlertTriangle,
        bg: "bg-amber-50",
        border: "border-amber-200",
        iconColor: "text-amber-600",
        iconBg: "bg-amber-100",
        cta: "View",
        estimate: () => null,
      };
  }
}

export function ActionQueue({ items }: ActionQueueProps) {
  const navigate = useNavigate();

  if (!items || items.length === 0) return null;

  return (
    <div className="space-y-2">
      {items.map((item) => {
        const config = getActionConfig(item.type);
        const Icon = config.icon;
        const estimate = config.estimate(item.count);

        return (
          <div
            key={item.type}
            className={`flex items-center justify-between rounded-xl border px-4 py-3.5 shadow-warm-sm cursor-pointer transition-shadow hover:shadow-warm-md ${config.bg} ${config.border}`}
            onClick={() => navigate(item.action)}
          >
            <div className="flex items-center gap-3">
              <div className={`flex h-9 w-9 items-center justify-center rounded-lg ${config.iconBg}`}>
                <Icon className={`h-4.5 w-4.5 ${config.iconColor}`} />
              </div>
              <div>
                <p className="text-sm font-semibold text-foreground">
                  {item.message}
                </p>
                {estimate && (
                  <p className="flex items-center gap-1 text-xs text-muted-foreground mt-0.5">
                    <Clock className="h-3 w-3" />
                    {estimate}
                  </p>
                )}
              </div>
            </div>
            <Button
              size="sm"
              variant="ghost"
              className="gap-1 font-medium"
              onClick={(e) => {
                e.stopPropagation();
                navigate(item.action);
              }}
            >
              {config.cta}
              <ArrowRight className="h-3.5 w-3.5" />
            </Button>
          </div>
        );
      })}
    </div>
  );
}
