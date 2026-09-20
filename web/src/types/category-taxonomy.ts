/** ISED income-statement section that groups the fixed taxonomy in the UI. */
export type IsedSection =
  | "revenue"
  | "cost_of_sales"
  | "operating"
  | "structural"
  | "uncategorized"; // UI-only pseudo-section for the fallback bucket in analytics

/** One canonical category as served by GET /api/transactions/categories. */
export interface CategoryOption {
  /** Exact canonical string, e.g. "Software and IT". This is the value stored on transactions. */
  name: string;
  /** income | cogs | expense | transfer | equity | tax | contra | none */
  kind: string;
  section: IsedSection;
  /** true for Opening/Closing inventory + Amortization — never a bank txn; hidden from manual pickers. */
  journal_only?: boolean;
  /** true ONLY for pure transfers: not shown as a P&L line. Owner's draw and tax ARE P&L lines. */
  excluded_from_pl?: boolean;
}

export interface CategoryGroup {
  section: IsedSection;
  /** Optgroup / analytics section label, e.g. "Operating expenses". */
  label: string;
  options: CategoryOption[];
}

/** Shape of GET /api/transactions/categories (API domain owns this contract). */
export interface CategoriesResponse {
  /** Flat, section-ordered list — kept for back-compat + value validation. */
  categories: string[];
  /** Grouped fixed taxonomy — source for optgroups and analytics section grouping. */
  groups: CategoryGroup[];
}

/** Fixed display metadata for the four real income-statement sections + the UI fallback. */
export const SECTION_META: Record<
  IsedSection,
  { pickerLabel: string; analyticsLabel: string; order: number; excludedFromPl: boolean }
> = {
  revenue:       { pickerLabel: "Revenue",            analyticsLabel: "Revenue",            order: 1, excludedFromPl: false },
  cost_of_sales: { pickerLabel: "Cost of sales",      analyticsLabel: "Cost of sales",      order: 2, excludedFromPl: false },
  operating:     { pickerLabel: "Operating expenses", analyticsLabel: "Operating expenses", order: 3, excludedFromPl: false },
  structural:    { pickerLabel: "Structural",         analyticsLabel: "Structural", order: 4, excludedFromPl: false }, // mixed: per-category excluded_from_pl (transfers only) is authoritative
  uncategorized: { pickerLabel: "Uncategorized",      analyticsLabel: "Uncategorized",      order: 5, excludedFromPl: false },
};
