import { useMemo, useState, useEffect, useRef, type ReactNode } from "react";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Search, X, SlidersHorizontal } from "lucide-react";
import { cn } from "@/lib/utils";
import { useAccountDisplayNames } from "@/hooks/use-account-names";

export interface TransactionFiltersState {
  account: string;
  status: string;
  month: string;
  search: string;
  min_amount: string;
  max_amount: string;
}

export interface DateRange {
  min_date: string | null;
  max_date: string | null;
}

interface TransactionFiltersProps {
  filters: TransactionFiltersState;
  onFiltersChange: (filters: TransactionFiltersState) => void;
  dateRange?: DateRange | null;
  /** Account values that actually exist in the user's uploaded data. */
  availableAccounts?: string[];
  /**
   * Optional action buttons rendered to the right of the search input
   * (e.g. "Recategorize"). Renders after the Clear filters button.
   */
  rightActions?: ReactNode;
}


const STATUSES = [
  { value: "", label: "All Status" },
  { value: "MATCHED", label: "Matched" },
  { value: "UNMATCHED", label: "Unmatched" },
  { value: "PENDING_REVIEW", label: "Pending Review" },
];

function generateMonthOptions(
  dateRange?: DateRange | null
): { value: string; label: string }[] {
  const options = [{ value: "", label: "All Months" }];
  const now = new Date();

  // Determine start date: use earliest transaction date if available, otherwise 12 months ago
  let start: Date;
  if (dateRange?.min_date) {
    const parts = dateRange.min_date.split("-");
    start = new Date(Number(parts[0]), Number(parts[1]) - 1, 1);
  } else {
    start = new Date(now.getFullYear(), now.getMonth() - 11, 1);
  }

  // Generate months from now back to start (inclusive)
  const d = new Date(now.getFullYear(), now.getMonth(), 1);
  while (d >= start) {
    const value = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
    const label = d.toLocaleDateString("en-CA", {
      year: "numeric",
      month: "long",
    });
    options.push({ value, label });
    d.setMonth(d.getMonth() - 1);
  }
  return options;
}

const PILL_ALL_SENTINEL = "__all__";

function PillSelect({
  value,
  onChange,
  options,
  isActive,
}: {
  value: string;
  onChange: (val: string) => void;
  options: { value: string; label: string }[];
  isActive: boolean;
}) {
  // Radix Select doesn't support empty-string values, so map "" to a sentinel
  const selectValue = value || PILL_ALL_SENTINEL;

  return (
    <Select
      value={selectValue}
      onValueChange={(v) => onChange(v === PILL_ALL_SENTINEL ? "" : v)}
    >
      <SelectTrigger
        className={cn(
          "h-9 rounded-full px-4 text-sm font-medium cursor-pointer w-auto gap-1.5",
          "border bg-card transition-all duration-150",
          isActive
            ? "border-primary/40 bg-primary/5 text-primary shadow-warm-sm"
            : "border-border/60 text-muted-foreground hover:border-primary/30 hover:bg-primary/[0.02]"
        )}
      >
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((opt) => (
          <SelectItem
            key={opt.value || PILL_ALL_SENTINEL}
            value={opt.value || PILL_ALL_SENTINEL}
          >
            {opt.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function TransactionFilters({
  filters,
  onFiltersChange,
  dateRange,
  availableAccounts,
  rightActions,
}: TransactionFiltersProps) {
  // Account key -> human name, from config/accounts.yaml on the server, with
  // institution-neutral fallbacks. No bank name is hard-coded in the bundle.
  const accountNames = useAccountDisplayNames();
  const accountOptions = useMemo(() => {
    const opts = [{ value: "", label: "All Accounts" }];
    if (availableAccounts && availableAccounts.length > 0) {
      for (const acct of availableAccounts) {
        opts.push({ value: acct, label: accountNames[acct] ?? acct });
      }
    } else {
      // Fallback: show every account the configuration knows about, for the
      // moment before the loaded data has said which ones exist.
      for (const [value, label] of Object.entries(accountNames)) {
        opts.push({ value, label });
      }
    }
    return opts;
  }, [availableAccounts, accountNames]);
  const monthOptions = useMemo(
    () => generateMonthOptions(dateRange),
    [dateRange?.min_date]
  );

  // Debounced text inputs — immediate UI update, 300ms debounce for API calls
  const [localSearch, setLocalSearch] = useState(filters.search);
  const [localMin, setLocalMin] = useState(filters.min_amount);
  const [localMax, setLocalMax] = useState(filters.max_amount);

  // Sync local state when parent resets filters (e.g. clear all)
  useEffect(() => {
    setLocalSearch(filters.search);
  }, [filters.search]);
  useEffect(() => {
    setLocalMin(filters.min_amount);
  }, [filters.min_amount]);
  useEffect(() => {
    setLocalMax(filters.max_amount);
  }, [filters.max_amount]);

  // Latest filters, read at debounce-fire time. Reading `filters` from the
  // effect closure would replay a stale snapshot and silently revert any
  // pill (account/status/month) changed while the timer was pending.
  const filtersRef = useRef(filters);
  useEffect(() => {
    filtersRef.current = filters;
  }, [filters]);

  // Debounce text inputs → parent
  useEffect(() => {
    const f = filtersRef.current;
    if (
      localSearch === f.search &&
      localMin === f.min_amount &&
      localMax === f.max_amount
    ) {
      return;
    }
    const timer = setTimeout(() => {
      onFiltersChange({
        ...filtersRef.current,
        search: localSearch,
        min_amount: localMin,
        max_amount: localMax,
      });
    }, 300);
    return () => clearTimeout(timer);
  }, [localSearch, localMin, localMax]); // eslint-disable-line react-hooks/exhaustive-deps

  const hasActiveFilters =
    filters.account || filters.status || filters.month || filters.search ||
    filters.min_amount || filters.max_amount;

  const activeFilterCount = [
    filters.account,
    filters.status,
    filters.month,
    filters.search,
    filters.min_amount || filters.max_amount ? "amount" : "",
  ].filter(Boolean).length;

  function updateFilter(key: keyof TransactionFiltersState, value: string) {
    onFiltersChange({ ...filters, [key]: value });
  }

  function clearFilters() {
    setLocalSearch("");
    setLocalMin("");
    setLocalMax("");
    onFiltersChange({ account: "", status: "", month: "", search: "", min_amount: "", max_amount: "" });
  }

  return (
    <div className="flex flex-wrap items-center gap-2.5">
      <div className="flex items-center gap-1.5 text-muted-foreground mr-1">
        <SlidersHorizontal className="h-4 w-4" />
        <span className="text-xs font-semibold uppercase tracking-wider hidden sm:inline">
          Filter
        </span>
      </div>

      <PillSelect
        value={filters.account}
        onChange={(val) => updateFilter("account", val)}
        options={accountOptions}
        isActive={!!filters.account}
      />

      <PillSelect
        value={filters.status}
        onChange={(val) => updateFilter("status", val)}
        options={STATUSES}
        isActive={!!filters.status}
      />

      <PillSelect
        value={filters.month}
        onChange={(val) => updateFilter("month", val)}
        options={monthOptions}
        isActive={!!filters.month}
      />

      {/* Amount range filter */}
      <div className="flex items-center gap-1.5">
        <div className="relative w-24">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-xs text-muted-foreground/60 font-medium">$</span>
          <Input
            type="number"
            placeholder="Min"
            value={localMin}
            onChange={(e) => setLocalMin(e.target.value)}
            className="pl-6 h-9 rounded-full border-border/60 bg-card text-sm focus-visible:ring-primary/30 focus-visible:border-primary/40 [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none [appearance:textfield]"
          />
        </div>
        <span className="text-xs text-muted-foreground">–</span>
        <div className="relative w-24">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-xs text-muted-foreground/60 font-medium">$</span>
          <Input
            type="number"
            placeholder="Max"
            value={localMax}
            onChange={(e) => setLocalMax(e.target.value)}
            className="pl-6 h-9 rounded-full border-border/60 bg-card text-sm focus-visible:ring-primary/30 focus-visible:border-primary/40 [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none [appearance:textfield]"
          />
        </div>
        {[
          { label: "$50+", value: "50" },
          { label: "$100+", value: "100" },
          { label: "$500+", value: "500" },
        ].map((preset) => (
          <button
            key={preset.value}
            onClick={() => setLocalMin(localMin === preset.value ? "" : preset.value)}
            className={cn(
              "h-7 px-2.5 rounded-full text-xs font-medium transition-all duration-150 whitespace-nowrap",
              localMin === preset.value
                ? "bg-primary/10 text-primary border border-primary/30"
                : "bg-muted/50 text-muted-foreground hover:bg-muted hover:text-foreground border border-transparent"
            )}
          >
            {preset.label}
          </button>
        ))}
      </div>

      <div className="relative flex-1 min-w-[200px] max-w-sm">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground/60" />
        <Input
          placeholder="Search every transaction — any field (description, amount, category, note, #ID, regex)…"
          value={localSearch}
          onChange={(e) => setLocalSearch(e.target.value)}
          className="pl-9 h-9 rounded-full border-border/60 bg-card focus-visible:ring-primary/30 focus-visible:border-primary/40"
        />
      </div>

      {hasActiveFilters && (
        <Button
          variant="ghost"
          size="sm"
          onClick={clearFilters}
          className="h-9 rounded-full text-muted-foreground hover:text-destructive hover:bg-destructive/5 gap-1.5"
        >
          <X className="h-3.5 w-3.5" />
          Clear
          <Badge className="bg-primary/10 text-primary border-primary/20 text-[10px] px-1.5 h-4 font-bold">
            {activeFilterCount}
          </Badge>
        </Button>
      )}

      {rightActions}
    </div>
  );
}
