import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface FxRate {
  rate: number;
  as_of: string;
}

export interface FxRatesResponse {
  base: string;
  rates: Record<string, FxRate>;
  missing: string[];
}

/** Daily CAD conversion rates for the given non-CAD currencies. */
export function useFxRates(currencies: string[]) {
  const nonCad = [...new Set(currencies.filter((c) => c !== "CAD"))].sort();
  return useQuery<FxRatesResponse>({
    queryKey: ["analytics", "fx-rates", nonCad],
    queryFn: () =>
      api.get(`/api/analytics/fx-rates?currencies=${nonCad.join(",")}`),
    enabled: nonCad.length > 0,
    staleTime: 12 * 60 * 60 * 1000, // BoC publishes once per business day
    retry: 1,
  });
}
