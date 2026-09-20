const ACCOUNT_TYPE_LABELS: Record<string, string> = {
  CHECKING: "Chequing",
  SAVINGS: "Savings",
  CREDIT_CARD: "Credit Card",
  FX_MULTI_CURRENCY: "Multi-Currency",
  MERCHANT: "Merchant",
  OTHER: "Other",
};

export function formatAccountType(type: string | null): string {
  if (!type) return "Unknown";
  return ACCOUNT_TYPE_LABELS[type] ?? type;
}

export function formatAccountDisplay(
  institution: string | null,
  type: string | null
): string {
  const inst = institution ?? "Unknown";
  const t = formatAccountType(type);
  return `${inst} ${t}`.trim();
}
