/** Formatting utilities for report display. */

export function formatCurrency(amount: number, currency?: string): string {
  const code = currency || "CAD";
  if (code === "CAD" || code === "USD") {
    return new Intl.NumberFormat("en-CA", {
      style: "currency",
      currency: code,
    }).format(amount);
  }
  // Never render a non-CAD/USD currency (e.g. EUR from Wise) with a dollar
  // sign — a €100 charge must not look identical to C$100.
  return `${amount.toLocaleString("en-CA", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })} ${code}`;
}

export function formatDate(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString("en-CA", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

export function formatDatetime(dateStr: string): string {
  const d = new Date(dateStr);
  return d.toLocaleString("en-CA", {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
