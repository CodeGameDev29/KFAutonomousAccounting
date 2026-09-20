import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface TransactionDateRange {
  min_date: string | null;
  max_date: string | null;
}

/** Earliest/latest transaction dates — anchors date presets to real data. */
export function useTransactionDateRange() {
  return useQuery<TransactionDateRange>({
    queryKey: ["transactions", "date-range"],
    queryFn: () => api.get("/api/transactions/date-range"),
    staleTime: 5 * 60 * 1000,
  });
}
