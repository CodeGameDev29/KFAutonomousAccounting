/** Merge param changes into a URLSearchParams copy; ""/null/undefined delete. */
export function withParams(
  base: URLSearchParams,
  changes: Record<string, string | null | undefined>,
): URLSearchParams {
  const next = new URLSearchParams(base);
  for (const [k, v] of Object.entries(changes)) {
    if (v === null || v === undefined || v === "") next.delete(k);
    else next.set(k, v);
  }
  return next;
}
