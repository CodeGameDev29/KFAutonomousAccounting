export interface CategoryBreakdown {
  category: string;
  currency: string;
  income: number;
  expenses: number;
  net: number;
  transaction_count: number;
}

export interface CategoryBreakdownResponse {
  categories: CategoryBreakdown[];
  default_period: { year: number; month: number } | null;
  start: string;
  end: string;
}
