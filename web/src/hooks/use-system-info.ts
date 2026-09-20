/**
 * The server's own account of how this instance is configured, read from
 * `GET /health` (public, same origin, no auth).
 *
 * Anything the UI would otherwise assert about an instance — which model reads
 * documents, where the confidence bands sit, how large an upload may be — is a
 * per-instance setting the server owns. A string literal in the bundle is wrong
 * on every instance configured differently, so ask the server instead. Every
 * reader below is allowed to answer "not known yet", and callers say nothing
 * rather than guess when it does.
 */

import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { API_URL } from "@/lib/constants";
import { setMaxUploadMb } from "@/lib/error-messages";

export interface LocalExtractionInfo {
  enabled: boolean;
  base_url: string;
  model: string;
  reachable: boolean;
}

export interface ExtractionInfo {
  /** Provider an upload would actually reach first: "local" | "gemini" | "openai" | null. */
  primary: string | null;
  primary_model?: string | null;
  local?: LocalExtractionInfo;
  gemini_key_present?: boolean;
  openai_key_present?: boolean;
  ladder?: string[];
}

/**
 * The reconciliation engine's own thresholds, as configured on this instance.
 *
 * All three confidence values are fractions in [0, 1]; the UI formats them as
 * percentages. They are env-tunable server-side, so the bundle must never
 * hard-code a band: a legend that says "85%+ is auto-approved" is simply false
 * on an instance whose floor is 0.90.
 */
export interface ReconciliationInfo {
  /** At or above this, the engine stores a match without asking. */
  auto_approve_threshold?: number;
  /** At or above this (and below auto-approve), the pair waits in Review. */
  review_threshold?: number;
  /** The floor "approve all" applies; pairs under it stay in Review. */
  bulk_approve_min_confidence?: number;
  /** Days either side of a transaction the matcher will look. */
  default_date_window_days?: number;
}

export interface LimitsInfo {
  /** Largest upload the server will accept, in megabytes. */
  max_upload_mb?: number;
}

export interface SystemInfo {
  status: string;
  storage?: string;
  extraction?: ExtractionInfo;
  reconciliation?: ReconciliationInfo;
  limits?: LimitsInfo;
  /**
   * Whether this instance will accept a new account, derived server-side from
   * `AUTH_ALLOW_SIGNUP` — the same value `POST /api/auth/signup` enforces.
   * Undefined on an older server, or before `/health` has answered.
   */
  signup_enabled?: boolean;
}

export function useSystemInfo() {
  return useQuery<SystemInfo>({
    queryKey: ["system", "health"],
    queryFn: async () => {
      const res = await fetch(`${API_URL}/health`, {
        headers: { Accept: "application/json" },
      });
      if (!res.ok) throw new Error(`/health returned ${res.status}`);
      return (await res.json()) as SystemInfo;
    },
    staleTime: 5 * 60 * 1000,
    // Two retries, not one: the first paint of a page that reads this is often
    // the first request after the server came up, and a single failure would
    // leave the page on its "nothing is known" fallback until a reload.
    retry: 2,
  });
}

/** A fraction in [0, 1] as a whole-percent string, or null when unknown. */
function asPercent(value: number | undefined): number | null {
  if (typeof value !== "number" || !Number.isFinite(value)) return null;
  // Tolerate a server that reports 85 rather than 0.85.
  const fraction = value > 1 ? value / 100 : value;
  if (fraction <= 0 || fraction > 1) return null;
  return Math.round(fraction * 100);
}

export interface ConfidenceBands {
  /** Percent at or above which the engine auto-approves. */
  high: number;
  /** Percent at or above which a pair is worth reviewing. */
  medium: number;
}

/**
 * The confidence bands this instance actually uses, or null while the server
 * has not said. Null means every caller shows the score and names no band —
 * never a hard-coded 85/60 that contradicts the engine.
 */
export function useConfidenceBands(): ConfidenceBands | null {
  const { data } = useSystemInfo();
  const high = asPercent(data?.reconciliation?.auto_approve_threshold);
  const medium = asPercent(data?.reconciliation?.review_threshold);
  if (high === null || medium === null || medium >= high) return null;
  return { high, medium };
}

/** The "approve all" floor as a whole percent, or null while unknown. */
export function useBulkApproveThreshold(): number | null {
  const { data } = useSystemInfo();
  return asPercent(data?.reconciliation?.bulk_approve_min_confidence);
}

/** The server's upload size limit in MB, or null while unknown. */
export function useMaxUploadMb(): number | null {
  const { data } = useSystemInfo();
  const mb = data?.limits?.max_upload_mb;
  return typeof mb === "number" && Number.isFinite(mb) && mb > 0 ? mb : null;
}

/**
 * Mounted once at the app root. Two jobs:
 *
 *  - it warms `/health` before any page needs it, so the Settings → Security
 *    cards render the server's real answer on their first paint instead of the
 *    "nothing has been reported yet" fallback;
 *  - it hands the upload limit to `lib/error-messages`, which is a plain module
 *    and cannot call a hook of its own. Until it answers, a 413 is worded
 *    without a number rather than with a guessed one.
 */
export function useInstanceInfoSync(): void {
  const maxUploadMb = useMaxUploadMb();
  useEffect(() => {
    setMaxUploadMb(maxUploadMb);
  }, [maxUploadMb]);
}

/**
 * Whether the sign-up form should be offered at all.
 *
 * Three states, and the difference matters: `true` open, `false` closed,
 * `undefined` not yet answered. A page that treats "not yet" as "closed" tells
 * someone the instance refuses them when it may not, so callers show a neutral
 * line while this is undefined. If `/health` cannot be reached at all, or
 * answers without the field, the answer stays undefined rather than guessing.
 */
export function useSignupEnabled(): boolean | undefined {
  const { data, isError } = useSystemInfo();
  if (isError) return undefined;
  return data?.signup_enabled;
}
