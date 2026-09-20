import { CheckCircle2, AlertTriangle, AlertCircle, XCircle } from "lucide-react";

interface ActionItem {
  type: string;
  count: number;
  message: string;
  action: string;
}

interface StatusBannerProps {
  actionItems: ActionItem[];
  hasConnectionIssues: boolean;
  monthLabel?: string;
}

type Severity = "error" | "warning" | "info" | "clear";

function computeSeverity(
  actionItems: ActionItem[],
  hasConnectionIssues: boolean
): { severity: Severity; message: string } {
  // Highest severity: connection errors
  if (hasConnectionIssues) {
    return {
      severity: "error",
      message: "A connected service needs attention",
    };
  }

  // Count action types
  const pendingReviews = actionItems.find((i) => i.type === "pending_reviews");
  const missingReceipts = actionItems.find((i) => i.type === "missing_receipts");
  const totalItems = actionItems.reduce((sum, i) => sum + i.count, 0);

  // Pending reviews = warning (needs human action)
  if (pendingReviews && pendingReviews.count > 0) {
    const otherCount = totalItems - pendingReviews.count;
    const extra = otherCount > 0 ? ` and ${otherCount} other item${otherCount !== 1 ? "s" : ""}` : "";
    return {
      severity: "warning",
      message: `${pendingReviews.count} match${pendingReviews.count !== 1 ? "es" : ""} need review${extra}`,
    };
  }

  // Missing receipts = info (less urgent)
  if (missingReceipts && missingReceipts.count > 0) {
    return {
      severity: "info",
      message: `${missingReceipts.count} transaction${missingReceipts.count !== 1 ? "s" : ""} missing receipts`,
    };
  }

  // Any other action items
  if (totalItems > 0) {
    return {
      severity: "info",
      message: `${totalItems} item${totalItems !== 1 ? "s" : ""} need attention`,
    };
  }

  return { severity: "clear", message: "" };
}

const severityConfig = {
  error: {
    icon: XCircle,
    bg: "bg-rose-50",
    border: "border-rose-200",
    iconColor: "text-rose-600",
    textColor: "text-rose-800",
  },
  warning: {
    icon: AlertTriangle,
    bg: "bg-amber-50",
    border: "border-amber-200",
    iconColor: "text-amber-600",
    textColor: "text-amber-800",
  },
  info: {
    icon: AlertCircle,
    bg: "bg-indigo-50",
    border: "border-primary/15",
    iconColor: "text-primary",
    textColor: "text-primary",
  },
  clear: {
    icon: CheckCircle2,
    bg: "bg-success/5",
    border: "border-success/20",
    iconColor: "text-success",
    textColor: "text-foreground",
  },
};

export function StatusBanner({
  actionItems,
  hasConnectionIssues,
  monthLabel,
}: StatusBannerProps) {
  const { severity, message } = computeSeverity(actionItems, hasConnectionIssues);
  const config = severityConfig[severity];
  const Icon = config.icon;

  if (severity === "clear") {
    const label = monthLabel ? ` through ${monthLabel}` : "";
    return (
      <div className={`flex items-center gap-3 rounded-xl border px-5 py-4 ${config.bg} ${config.border}`}>
        <Icon className={`h-5 w-5 ${config.iconColor}`} />
        <p className={`text-sm font-medium ${config.textColor}`}>
          Your books are up to date{label}
        </p>
      </div>
    );
  }

  return (
    <div className={`flex items-center gap-3 rounded-xl border px-5 py-4 ${config.bg} ${config.border}`}>
      <Icon className={`h-5 w-5 shrink-0 ${config.iconColor}`} />
      <p className={`text-sm font-medium ${config.textColor}`}>
        {message}
      </p>
    </div>
  );
}

