/** Shared money formatting + validated chart-mark palette. */

// Chart-mark palette (validated for lightness band, chroma floor, CVD
// separation, and surface contrast). The brand sage token is too gray to
// carry a fill, so charts use this snapped variant; text keeps brand tokens.
export const CHART_GREEN = "#53975E";
export const CHART_AMBER = "#D97706";

/** "$1,234.56" / "−$1,234.56" (true minus sign), currency-aware (en-CA). */
export function formatSignedMoney(value: number, currency: string): string {
  const abs = Math.abs(value);
  const formatted = new Intl.NumberFormat("en-CA", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
  }).format(abs);
  return value < 0 ? `−${formatted}` : formatted;
}
