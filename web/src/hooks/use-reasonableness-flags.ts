import {
  useQuery,
  useMutation,
  useQueryClient,
  keepPreviousData,
} from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  ReasonablenessResponse,
  DismissFlagResponse,
} from "@/types/reasonableness";

/**
 * Peer-benchmark ("reasonableness") flags for the Analytics card. Shares the
 * Analytics start/end and refetches on the next visit (5-min stale). Same
 * null-gate convention as useCategoryBreakdown.
 */
export function useReasonablenessFlags(
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

  return useQuery<ReasonablenessResponse>({
    queryKey: ["analytics", "reasonableness", startDate, endDate],
    queryFn: () => api.get(`/api/reasonableness/flags${qs}`),
    staleTime: 5 * 60 * 1000,
    enabled,
    placeholderData: keepPreviousData,
  });
}

/**
 * Toggle a flag's dismissed state, then invalidate the flags query so the
 * card re-renders without the dismissed flag (and with the recomputed band).
 */
export function useDismissFlag(
  startDate: string | null,
  endDate: string | null,
) {
  const queryClient = useQueryClient();
  return useMutation<
    DismissFlagResponse,
    Error,
    { id: string; dismissed: boolean }
  >({
    mutationFn: ({ id, dismissed }) =>
      api.post(`/api/reasonableness/flags/${encodeURIComponent(id)}/dismiss`, {
        dismissed,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({
        queryKey: ["analytics", "reasonableness", startDate, endDate],
      });
    },
  });
}
