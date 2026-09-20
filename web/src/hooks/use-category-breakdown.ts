import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { CategoryBreakdownResponse } from "@/types/analytics";

export function useCategoryBreakdown(
  startDate: string | null,
  endDate: string | null,
  options?: { enabled?: boolean },
) {
  const bothProvided = startDate !== null && endDate !== null;
  const bothMissing = startDate === null && endDate === null;
  const enabled = (bothProvided || bothMissing) && (options?.enabled ?? true);

  const qs = bothProvided
    ? `?start=${encodeURIComponent(startDate)}&end=${encodeURIComponent(endDate)}`
    : "";

  return useQuery<CategoryBreakdownResponse>({
    queryKey: ["analytics", "category-breakdown", startDate, endDate],
    queryFn: () => api.get(`/api/analytics/category-breakdown${qs}`),
    staleTime: 5 * 60 * 1000,
    enabled,
    placeholderData: keepPreviousData,
  });
}
