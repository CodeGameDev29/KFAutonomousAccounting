import { Upload, Play } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { ContextualPrompt } from "@/components/shared/ContextualPrompt";
import { api } from "@/lib/api";
import { useAuth } from "@/hooks/use-auth";

interface DashboardSummary {
  total_receipts?: number;
  total_transactions?: number;
  unmatched_count?: number;
}

export function TransactionsPagePrompt() {
  // Reads go through the shared `api` helper, which attaches the access token
  // and handles the 401 refresh-and-retry. A hand-rolled fetch here could not:
  // `useAuth()` exposes no token to put in an Authorization header.
  const { isAuthenticated } = useAuth();

  const { data: summary } = useQuery<DashboardSummary | null>({
    queryKey: ["dashboard", "summary"],
    queryFn: () => api.get<DashboardSummary>("/api/dashboard/summary"),
    enabled: isAuthenticated,
  });

  if (!summary) return null;

  const totalTx = (summary.total_receipts ?? 0) + (summary.total_transactions ?? 0);

  if (totalTx === 0) {
    return (
      <ContextualPrompt
        icon={Upload}
        message="Upload receipts and bank statements to get started."
        cta={{ label: "Upload", to: "/transactions" }}
        variant="primary"
      />
    );
  }

  if ((summary.unmatched_count ?? 0) > 0) {
    return (
      <ContextualPrompt
        icon={Play}
        message={`${summary.unmatched_count} unmatched transactions. Start reconciliation to match them.`}
        cta={{ label: "Start Reconciliation", to: "/review" }}
        variant="primary"
      />
    );
  }

  return null;
}
