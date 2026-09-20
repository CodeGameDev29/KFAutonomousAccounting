export type ReasonablenessBand = "typical" | "few" | "several";
export type FlagSeverity = "stands_out" | "elevated" | "info";
export type FlagDirection = "high" | "low" | "n/a";
export type ReasonablenessStatus = "ok" | "missing_naics" | "below_floor" | "no_data";
export type ComparisonStatus =
  | "in_line"
  | "above"
  | "below"
  | "well_above"
  | "well_below"
  | "no_benchmark"
  | "watch";

export interface ReasonablenessComparison {
  label: string;
  category: string;
  gifi_bucket: string;
  your_pct: number;
  // median-first central "typical" value; null => suppressed / no line
  peer_pct: number | null;
  // honest size-cohort range [low, high]; null => no reliable band
  peer_band: [number, number] | null;
  amount: number;
  currency: string;
  direction: FlagDirection;
  status: ComparisonStatus;
  cra_scrutinized: boolean;
  flag_key: string | null;
  dismissed: boolean;
  // Industry-benchmark fields, 1:1 with the server's Pydantic model.
  business_count: number | null;
  quality: "A" | "B" | "C" | "D" | "E" | null;
  recommendation: string | null;
  unavailable_reason: "grouped_other" | "insufficient_filings" | null;
  weight: number;
}

export interface ReasonablenessFlag {
  id: string;
  category: string;
  severity: FlagSeverity;
  direction: FlagDirection;
  title: string;
  prompt: string;
  your_pct: number | null;
  peer_pct: number | null;
  peer_band: [number, number] | null;
  amount: number | null;
  currency: string;
  dismissed: boolean;
  // Industry-benchmark fields.
  business_count: number | null;
  recommendation: string | null;
}

export interface ReasonablenessMeta {
  naics_code: string | null;
  naics_source: "profile" | "industry_map" | "data_inferred" | null;
  naics_match_depth: number | null;
  revenue_band: string | null;
  quartile: string | null;
  benchmark_year: number | null;
  operating_revenue: number;
  currency: string;
  period_months: number;
  partial_year: boolean;
  below_floor: boolean;
  industry_label: string | null;
  attribution: string;
  dismissed_flag_keys?: string[];
  // Source-data disclosure — what the user is compared against.
  cohort_label?: string | null;
  revenue_band_label?: string | null;
  geography?: string | null;
  province?: string | null;
  cohort_province?: string | null;
  sample_size?: number | null;
  pct_profitable?: number | null;
  gross_margin?: number | null;
  net_profit_margin?: number | null;
  annualized_revenue?: number | null;
  source_url?: string | null;
  source_tool_url?: string | null;
  source_dataset_url?: string | null;
  licence_url?: string | null;
  // Industry-benchmark fields, 1:1 with the server's Pydantic model.
  size_cohort: string | null;
  size_cohort_label: string | null;
  cohort_quality: "A" | "B" | "C" | "D" | "E" | null;
  // real N for the provenance chip (cohort-level)
  business_count: number | null;
  cohort_trail: {
    naics_depth: number;
    band: string | null;
    geo: "provincial" | "national" | null;
    cohort: string | null;
  } | null;
  data_vintage: number | null;
}

// Always-on profitability per-line comparison.
// Peer-DESCRIPTIVE only: no user-rank / user-margin / status field by design.
export interface ProfitabilityLine {
  label: string;
  gifi_bucket: string;
  your_pct: number;
  peer_pct: number;
  business_count: number;
  note: string;
}

export interface ReasonablenessResponse {
  status: ReasonablenessStatus;
  band: ReasonablenessBand | null;
  band_label: string | null;
  flags: ReasonablenessFlag[];
  comparisons: ReasonablenessComparison[];
  reassurances: string[];
  meta: ReasonablenessMeta;
  // Always-on profitability cohort (empty => section hidden).
  profitability: ProfitabilityLine[];
  profitability_cohort_label: string | null;
  start: string;
  end: string;
}

export interface DismissFlagResponse {
  id: string;
  dismissed: boolean;
}
