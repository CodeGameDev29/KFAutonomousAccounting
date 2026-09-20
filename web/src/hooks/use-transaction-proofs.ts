import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

// ─── Types ─────────────────────────────────────────────────────────────────

export interface TransactionProof {
  proof_id: number;
  document_id: number | null;
  proof_type: "document" | "transaction_link";
  label: string;
  confidence_score: number | null;
  amount: string | null;
  currency: string | null;
  date: string | null;
  vendor: string | null;
  status: string;
  match_source: string;
  stored_path: string | null;
  created_at: string | null;
}

export interface ProofCoverage {
  transaction_amount: string;
  transaction_currency: string;
  /** Sum of proofs in the transaction's own currency only. */
  total_proof_amount: string;
  /** null = not computable (all valued proofs are in another currency). */
  coverage_ratio: number | null;
  is_fully_covered: boolean | null;
  /** Proofs whose currency differs from the transaction. */
  cross_currency_count?: number;
  proof_count: number;
  document_count: number;
  link_count: number;
}

export interface TransactionProofsResponse {
  transaction_id: number;
  proofs: TransactionProof[];
  coverage: ProofCoverage;
}

// ─── Hooks ─────────────────────────────────────────────────────────────────

const INVALIDATE_KEYS = [
  ["transaction-proofs"],
  ["transactions"],
  ["transaction"],
  ["receipts"],
  ["dashboard"],
  ["review-match-counts"],
];

export function useTransactionProofs(txnId: number | null) {
  return useQuery<TransactionProofsResponse>({
    queryKey: ["transaction-proofs", txnId],
    queryFn: () => api.get<TransactionProofsResponse>(`/api/transactions/${txnId}/proofs`),
    enabled: txnId != null,
    staleTime: 30_000,
  });
}

export function useAddProof() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      txnId,
      documentIds,
      groupId,
    }: {
      txnId: number;
      documentIds: number[];
      groupId?: string;
    }) =>
      api.post(`/api/transactions/${txnId}/proofs`, {
        document_ids: documentIds,
        group_id: groupId,
      }),
    onSuccess: async () => {
      await Promise.all(
        INVALIDATE_KEYS.map((key) => qc.invalidateQueries({ queryKey: key }))
      );
    },
  });
}

export function useRemoveProof() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ txnId, proofId }: { txnId: number; proofId: number }) =>
      api.delete(`/api/transactions/${txnId}/proofs/${proofId}`),
    onSuccess: async () => {
      await Promise.all(
        INVALIDATE_KEYS.map((key) => qc.invalidateQueries({ queryKey: key }))
      );
    },
  });
}
