/** TanStack Query hooks for cross-statement transaction linking. */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import type {
  TransactionChainResponse,
  LinkCandidate,
  CreateLinkRequest,
  CreateLinkResponse,
  TransactionLinksResponse,
} from "@/types/transaction-links";

/** Every link on a transaction, plus the offset arithmetic for the group. */
export function useTransactionLinks(txnId: number | null) {
  return useQuery({
    queryKey: ["transaction-links", txnId],
    queryFn: () =>
      api.get<TransactionLinksResponse>(`/api/transactions/${txnId}/links`),
    enabled: txnId != null,
  });
}

export function useTransactionChain(txnId: number | null) {
  return useQuery({
    queryKey: ["transaction-chain", txnId],
    queryFn: () => api.get<TransactionChainResponse>(`/api/transactions/${txnId}/chain`),
    enabled: txnId != null,
  });
}

export function useLinkCandidates(txnId: number | null, search: string) {
  // A search reaches the user's ENTIRE history server-side; return more rows
  // so a specific match isn't hidden behind the score-sorted top of the list.
  const limit = search.trim() ? 50 : 30;
  return useQuery({
    queryKey: ["link-candidates", txnId, search],
    queryFn: () =>
      api.get<{ candidates: LinkCandidate[]; total: number }>(
        `/api/transactions/${txnId}/available-transaction-matches?search=${encodeURIComponent(search)}&limit=${limit}`
      ),
    enabled: txnId != null,
    // Always refetch on dialog open so freshly-uploaded statements are
    // visible without waiting for cache expiry.
    staleTime: 0,
    refetchOnMount: "always",
    refetchOnWindowFocus: true,
  });
}

/** Invalidate every view that renders linkage. */
function useInvalidateLinks() {
  const queryClient = useQueryClient();
  return () => {
    for (const key of [
      "transactions",
      "transaction",
      "transaction-links",
      "transaction-chain",
      "link-candidates",
      "dashboard",
    ]) {
      queryClient.invalidateQueries({ queryKey: [key] });
    }
  };
}

/** Link an anchor transaction to one or many counterparts in a single call. */
export function useCreateLink() {
  const invalidate = useInvalidateLinks();
  return useMutation({
    mutationFn: (body: CreateLinkRequest) =>
      api.post<CreateLinkResponse>("/api/transactions/links", body),
    onSuccess: (data) => {
      const n = data?.links?.length ?? 1;
      toast.success(`Linked ${n} transaction${n === 1 ? "" : "s"}.`);
      invalidate();
    },
  });
}

export function useDeleteLink() {
  const invalidate = useInvalidateLinks();
  return useMutation({
    mutationFn: (linkId: number) => api.delete(`/api/transactions/links/${linkId}`),
    onSuccess: () => {
      toast.success("Link removed.");
      invalidate();
    },
  });
}

/** Remove every leg of a fan-out group at once. */
export function useDeleteLinkGroup() {
  const invalidate = useInvalidateLinks();
  return useMutation({
    mutationFn: (chainId: string) =>
      api.delete(`/api/transactions/link-groups/${chainId}`),
    onSuccess: () => {
      toast.success("Link group removed.");
      invalidate();
    },
  });
}

export function useUpdateLinkStatus() {
  const invalidate = useInvalidateLinks();
  return useMutation({
    mutationFn: ({ linkId, action }: { linkId: number; action: "approve" | "reject" }) =>
      api.patch(`/api/transactions/links/${linkId}`, { action }),
    onSuccess: (_data, { action }) => {
      toast.success(action === "approve" ? "Link approved." : "Link rejected.");
      invalidate();
    },
  });
}

export function useTransactionPage(txnId: number | null, sourceFile?: string, perPage?: number) {
  return useQuery({
    queryKey: ["transaction-page", txnId, sourceFile, perPage],
    queryFn: () => {
      const params = new URLSearchParams();
      if (sourceFile) params.set("source_file", sourceFile);
      if (perPage) params.set("per_page", String(perPage));
      return api.get<{ page: number; row_number: number; total_count: number; per_page: number }>(
        `/api/transactions/${txnId}/page?${params.toString()}`
      );
    },
    enabled: txnId != null,
  });
}
