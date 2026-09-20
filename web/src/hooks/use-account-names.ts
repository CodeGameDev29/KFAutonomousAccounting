import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface AccountDisplayNames {
  display: Record<string, string>;
  order: string[];
}

/**
 * Generic fallbacks, used until the server answers and whenever
 * `config/accounts.yaml` is missing or does not name an account.
 *
 * Deliberately institution-neutral: the bundle names no particular bank. The
 * real names come from `config/accounts.yaml`, the one place they are edited,
 * which the XLSX binder and the PDF/HTML reports already read.
 */
export const FALLBACK_ACCOUNT_NAMES: Record<string, string> = {
  CAD: "CAD Chequing",
  USD: "USD Chequing",
  Wise: "Wise",
  CreditCard: "Credit Card",
  PayPal: "PayPal",
  Amazon: "Amazon",
};

/** `config/accounts.yaml`'s display map, read from the server. */
export function useAccountDisplayNames(): Record<string, string> {
  const { data } = useQuery<AccountDisplayNames>({
    queryKey: ["accounts", "display-names"],
    queryFn: () => api.get("/api/accounts/display-names"),
    staleTime: 30 * 60 * 1000,
  });
  // Stable identity between renders so callers can use it as a memo dependency.
  return useMemo(
    () => ({ ...FALLBACK_ACCOUNT_NAMES, ...(data?.display ?? {}) }),
    [data]
  );
}
