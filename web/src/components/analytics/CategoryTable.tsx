import { useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { withParams } from "@/lib/url-state";
import { useFxRates } from "@/hooks/use-fx-rates";
import {
  ArrowDownRight,
  ArrowUpRight,
  ArrowLeftRight,
  Boxes,
  Briefcase,
  Building2,
  Car,
  ChevronRight,
  Coins,
  CreditCard,
  DollarSign,
  HandCoins,
  HelpCircle,
  Landmark,
  Laptop,
  Megaphone,
  MoreHorizontal,
  Package,
  PackageOpen,
  Paperclip,
  Plane,
  Receipt,
  RefreshCcw,
  Scale,
  ShieldCheck,
  TrendingDown,
  Truck,
  Users,
  UtensilsCrossed,
  Wrench,
  Zap,
  type LucideIcon,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import {
  CHART_AMBER,
  CHART_GREEN,
  formatSignedMoney,
} from "@/lib/money";
import { useCategoryTaxonomy } from "@/hooks/use-category-taxonomy";
import { SECTION_META, type IsedSection } from "@/types/category-taxonomy";
import type { CategoryBreakdown } from "@/types/analytics";

const CATEGORY_ICONS: Record<string, LucideIcon> = {
  // Revenue
  "Sales of goods and services": DollarSign,
  "All other revenues": Coins,
  // Cost of sales
  "Wages and benefits": Users,
  "Purchases, materials and sub-contracts": Package,
  "Opening inventory": PackageOpen,
  "Closing inventory": Boxes,
  // Operating
  "Labour and commissions": Briefcase,
  "Amortization and depletion": TrendingDown,
  "Repairs and maintenance": Wrench,
  "Utilities and telephone/telecommunication": Zap,
  "Rent": Building2,
  "Interest and bank charges": Landmark,
  "Professional and business fees": Scale,
  "Advertising and promotion": Megaphone,
  "Delivery, shipping and warehouse expenses": Truck,
  "Insurance": ShieldCheck,
  "Travel": Plane,
  "Meals and entertainment": UtensilsCrossed,
  "Office supplies": Paperclip,
  "Software and IT": Laptop,
  "Motor vehicle expenses": Car,
  "Other indirect expenses": MoreHorizontal,
  // Structural
  "Credit card payment": CreditCard,
  "Internal transfer": ArrowLeftRight,
  "Owner's draw": HandCoins,
  "Income tax and GST/HST remittance": Receipt,
  "Refund/Credit": RefreshCcw,
  // Fallback bucket
  "Uncategorized": HelpCircle,
};

const formatMoney = formatSignedMoney;

interface Row extends CategoryBreakdown {
  share: number; // 0-1 of its group's absolute net
  /** undefined = comparison unknown (loading/failed); null = no activity in comparison window. */
  prevNet: number | null | undefined;
}

interface Group {
  /** ISED section for taxonomy grouping, or "in"/"out" for the load-time fallback. */
  key: string;
  /** Resolved ISED section; undefined for the money-in/out load fallback. */
  section?: IsedSection;
  title: string;
  rows: Row[];
  total: number;
}

function buildGroups(
  categories: CategoryBreakdown[],
  prevCategories: CategoryBreakdown[] | undefined,
): Group[] {
  const prevKnown = prevCategories !== undefined;
  const prevMap = new Map<string, number>();
  for (const p of prevCategories ?? []) {
    prevMap.set(`${p.category}::${p.currency}`, p.net);
  }
  const withPrev = (c: CategoryBreakdown): Omit<Row, "share"> => ({
    ...c,
    prevNet: prevKnown
      ? (prevMap.get(`${c.category}::${c.currency}`) ?? null)
      : undefined,
  });

  // Zero-net categories (washes) go with whichever side dominates their
  // gross activity, so a balanced category isn't mislabeled as spending.
  const isOut = (c: CategoryBreakdown) =>
    c.net < -0.005 || (Math.abs(c.net) <= 0.005 && c.expenses >= c.income);
  const inRows = categories.filter((c) => !isOut(c)).map(withPrev);
  const outRows = categories.filter(isOut).map(withPrev);

  const finish = (
    rows: Array<Omit<Row, "share">>,
    key: "in" | "out",
    title: string,
  ): Group => {
    const total = rows.reduce((s, r) => s + Math.abs(r.net), 0);
    const withShare = rows
      .map((r) => ({ ...r, share: total > 0 ? Math.abs(r.net) / total : 0 }))
      .sort((a, b) => Math.abs(b.net) - Math.abs(a.net));
    return { key, title, rows: withShare, total };
  };

  const groups: Group[] = [];
  if (inRows.length > 0) groups.push(finish(inRows, "in", "Money in"));
  if (outRows.length > 0) groups.push(finish(outRows, "out", "Money out"));
  return groups;
}

/**
 * Group rows by ISED income-statement section (Revenue → Cost of sales →
 * Operating → Structural → Uncategorized), ordered by SECTION_META.order.
 * Falls back to money-in / money-out grouping while the taxonomy is still
 * loading so the table never renders empty.
 */
function buildSections(
  categories: CategoryBreakdown[],
  prevCategories: CategoryBreakdown[] | undefined,
  sectionOf: (name: string | null | undefined) => IsedSection,
  taxonomyLoaded: boolean,
): Group[] {
  if (!taxonomyLoaded) {
    return buildGroups(categories, prevCategories);
  }

  const prevKnown = prevCategories !== undefined;
  const prevMap = new Map<string, number>();
  for (const p of prevCategories ?? []) {
    prevMap.set(`${p.category}::${p.currency}`, p.net);
  }
  const withPrev = (c: CategoryBreakdown): Omit<Row, "share"> => ({
    ...c,
    prevNet: prevKnown
      ? (prevMap.get(`${c.category}::${c.currency}`) ?? null)
      : undefined,
  });

  const bySection = new Map<IsedSection, Array<Omit<Row, "share">>>();
  for (const c of categories) {
    const section = sectionOf(c.category);
    const arr = bySection.get(section);
    if (arr) arr.push(withPrev(c));
    else bySection.set(section, [withPrev(c)]);
  }

  const finish = (
    rows: Array<Omit<Row, "share">>,
    section: IsedSection,
  ): Group => {
    const total = rows.reduce((s, r) => s + Math.abs(r.net), 0);
    const withShare = rows
      .map((r) => ({ ...r, share: total > 0 ? Math.abs(r.net) / total : 0 }))
      .sort((a, b) => Math.abs(b.net) - Math.abs(a.net));
    return {
      key: section,
      section,
      title: SECTION_META[section].analyticsLabel,
      rows: withShare,
      total,
    };
  };

  const ordered = (Object.keys(SECTION_META) as IsedSection[]).sort(
    (a, b) => SECTION_META[a].order - SECTION_META[b].order,
  );

  const groups: Group[] = [];
  for (const section of ordered) {
    const rows = bySection.get(section);
    if (rows && rows.length > 0) groups.push(finish(rows, section));
  }
  return groups;
}

function DeltaCell({
  row,
  compareLabel,
  approx = false,
}: {
  row: Row;
  compareLabel: string;
  approx?: boolean;
}) {
  if (row.prevNet === undefined) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  if (row.prevNet === null) {
    return (
      <span className="text-xs text-muted-foreground" title={`No activity in ${compareLabel}`}>
        new
      </span>
    );
  }
  const delta = row.net - row.prevNet;
  if (Math.abs(delta) < 0.005) {
    return <span className="text-xs text-muted-foreground">—</span>;
  }
  // For spending rows net is negative, so a *lower* net means *more* spent.
  const isExpenseRow = row.net <= 0.005;
  const magnitude = Math.abs(delta);
  const moreActivity = isExpenseRow ? delta < 0 : delta > 0;
  const good = isExpenseRow ? !moreActivity : moreActivity;
  const Icon = moreActivity ? ArrowUpRight : ArrowDownRight;
  const verb = isExpenseRow
    ? moreActivity
      ? "more spent"
      : "less spent"
    : moreActivity
      ? "more"
      : "less";
  return (
    <span
      className={cn(
        "inline-flex items-center gap-0.5 text-xs font-medium whitespace-nowrap",
        good ? "text-success" : "text-accent",
      )}
      title={`${approx ? "≈" : ""}${formatMoney(magnitude, row.currency)} ${verb} than ${compareLabel}`}
    >
      <Icon className="h-3 w-3" />
      {approx ? "≈" : ""}
      {formatMoney(magnitude, row.currency)}
    </span>
  );
}

interface CategoryTableProps {
  categories: CategoryBreakdown[];
  prevCategories: CategoryBreakdown[] | undefined;
  compareLabel: string;
  onCategoryClick: (category: string) => void;
}

interface Section {
  key: string;
  title: string;
  currency: string;
  approx: boolean;
  caption?: string;
  groups: Group[];
}

/** Convert every row to CAD and merge same-named categories across currencies. */
function convertAndMerge(
  list: CategoryBreakdown[],
  rateFor: (currency: string) => number | undefined,
): CategoryBreakdown[] {
  const map = new Map<string, CategoryBreakdown>();
  for (const c of list) {
    const rate = rateFor(c.currency);
    if (rate === undefined) continue;
    const cur = map.get(c.category) ?? {
      category: c.category,
      currency: "CAD",
      income: 0,
      expenses: 0,
      net: 0,
      transaction_count: 0,
    };
    cur.income += c.income * rate;
    cur.expenses += c.expenses * rate;
    cur.net += c.net * rate;
    cur.transaction_count += c.transaction_count;
    map.set(c.category, cur);
  }
  return [...map.values()];
}

export function CategoryTable({
  categories,
  prevCategories,
  compareLabel,
  onCategoryClick,
}: CategoryTableProps) {
  const [searchParams, setSearchParams] = useSearchParams();
  const { sectionOf, groups: taxonomyGroups } = useCategoryTaxonomy();
  const taxonomyLoaded = taxonomyGroups.length > 0;

  const currencies = useMemo(() => {
    const list = [...new Set(categories.map((c) => c.currency))];
    return list.sort((a, b) => {
      if (a === "CAD") return -1;
      if (b === "CAD") return 1;
      return a.localeCompare(b);
    });
  }, [categories]);
  const multiCurrency = currencies.length > 1;

  const fx = useFxRates(
    useMemo(
      () => [
        ...new Set([
          ...categories.map((c) => c.currency),
          ...(prevCategories ?? []).map((c) => c.currency),
        ]),
      ],
      [categories, prevCategories],
    ),
  );
  const rateFor = (currency: string): number | undefined =>
    currency === "CAD" ? 1 : fx.data?.rates[currency]?.rate;
  const allRates = currencies.every((c) => rateFor(c) !== undefined);

  // View selection lives in the URL: absent = combined (≈ CAD), or one currency.
  const ccurParam = searchParams.get("ccur");
  const view =
    ccurParam && currencies.includes(ccurParam) ? ccurParam : "combined";
  const setView = (v: string) =>
    setSearchParams((prev) =>
      withParams(prev, { ccur: v === "combined" ? null : v }),
    );

  const sections = useMemo<Section[]>(() => {
    const perCurrency = (currency: string, caption?: string): Section => ({
      key: currency,
      title: multiCurrency ? `Categories · ${currency}` : "Categories",
      currency,
      approx: false,
      caption,
      groups: buildSections(
        categories.filter((c) => c.currency === currency),
        prevCategories?.filter((c) => c.currency === currency),
        sectionOf,
        taxonomyLoaded,
      ),
    });

    if (!multiCurrency) {
      return currencies.map((c) => perCurrency(c));
    }
    if (view !== "combined") {
      return [perCurrency(view)];
    }
    if (allRates) {
      const rateCaption = Object.entries(fx.data?.rates ?? {})
        .map(([c, r]) => `1 ${c} ≈ ${r.rate.toFixed(4)} CAD (Bank of Canada, ${r.as_of})`)
        .join(" · ");
      return [
        {
          key: "combined",
          title: "Categories · All currencies",
          currency: "CAD",
          approx: true,
          caption: rateCaption,
          groups: buildSections(
            convertAndMerge(categories, rateFor),
            prevCategories
              ? convertAndMerge(prevCategories, rateFor)
              : undefined,
            sectionOf,
            taxonomyLoaded,
          ),
        },
      ];
    }
    // Combined requested but a rate is unavailable — show per-currency
    // rather than a knowingly wrong merged total.
    return currencies.map((c) =>
      perCurrency(
        c,
        "Exchange rate unavailable right now — showing each currency separately.",
      ),
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [categories, prevCategories, currencies, multiCurrency, view, allRates, fx.data, sectionOf, taxonomyLoaded]);

  const currencyPicker = multiCurrency ? (
    <select
      value={view}
      onChange={(e) => setView(e.target.value)}
      aria-label="Currency view"
      className="h-8 rounded-md border border-border/60 bg-background px-2 text-xs font-medium focus:outline-none focus:ring-1 focus:ring-primary/50"
    >
      <option value="combined">All currencies (≈ CAD)</option>
      {currencies.map((c) => (
        <option key={c} value={c}>
          {c} only
        </option>
      ))}
    </select>
  ) : null;

  return (
    <div className="space-y-4">
      {sections.map(({ key, title, currency, approx, caption, groups }, i) => {
        const pre = approx ? "≈" : "";
        return (
          <Card key={key} className="shadow-warm rounded-xl">
            <CardHeader className="pb-3">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <CardTitle className="text-lg font-bold">{title}</CardTitle>
                {i === 0 && currencyPicker}
              </div>
              {caption && (
                <p className="text-[11px] text-muted-foreground">{caption}</p>
              )}
            </CardHeader>
            <CardContent className="pt-0">
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-[11px] uppercase tracking-wider text-muted-foreground border-b border-border/60">
                      <th className="py-2 pr-3 font-semibold">Category</th>
                      <th className="py-2 pr-3 font-semibold w-[26%] hidden sm:table-cell">
                        Share
                      </th>
                      <th className="py-2 pr-3 font-semibold text-right">
                        Amount
                      </th>
                      <th className="py-2 pr-3 font-semibold text-right whitespace-nowrap">
                        vs {compareLabel}
                      </th>
                      <th className="py-2 pr-1 font-semibold text-right">
                        Txns
                      </th>
                      <th className="w-6" aria-hidden />
                    </tr>
                  </thead>
                  {groups.map((group) => {
                    const signedTotal = group.rows.reduce(
                      (s, r) => s + r.net,
                      0,
                    );
                    return (
                    <tbody key={group.key}>
                      <tr>
                        <td
                          colSpan={6}
                          className="pt-4 pb-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground"
                        >
                          {group.title}
                          <span className="ml-2 font-mono normal-case tracking-normal text-foreground">
                            {pre}
                            {formatMoney(signedTotal, currency)}
                          </span>
                        </td>
                      </tr>
                      {group.key === "structural" && (
                        <tr>
                          <td
                            colSpan={6}
                            className="pb-2 text-[11px] leading-snug text-muted-foreground"
                          >
                            Credit-card payments and internal transfers are
                            excluded from the P&L (they would double-count);
                            refunds are shown as signed offsets. All remain in
                            your raw transaction totals and money-in / money-out
                            figures. (Owner's draw and income tax / GST-HST
                            remittances are now counted under Operating
                            expenses.)
                          </td>
                        </tr>
                      )}
                      {group.rows.map((row) => {
                        const Icon =
                          CATEGORY_ICONS[row.category] ?? HelpCircle;
                        const barColor =
                          row.net >= 0 ? CHART_GREEN : CHART_AMBER;
                        const isUncategorized =
                          row.category === "Uncategorized";
                        return (
                          <tr
                            key={`${row.category}-${row.currency}`}
                            role="button"
                            tabIndex={0}
                            aria-label={`${row.category}: ${formatMoney(row.net, row.currency)} from ${row.transaction_count} transactions. View transactions.`}
                            onClick={() => onCategoryClick(row.category)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter" || e.key === " ") {
                                e.preventDefault();
                                onCategoryClick(row.category);
                              }
                            }}
                            className="border-b border-border/40 last:border-0 cursor-pointer transition-colors hover:bg-muted/40 focus-visible:outline-none focus-visible:bg-muted/40"
                          >
                            <td className="py-2.5 pr-3">
                              <span className="flex items-center gap-2 min-w-0">
                                <Icon className="h-4 w-4 shrink-0 text-muted-foreground" />
                                <span className="font-medium text-foreground truncate">
                                  {row.category}
                                </span>
                                {isUncategorized && (
                                  <span className="shrink-0 rounded-full bg-amber-100 border border-amber-200 px-2 py-0.5 text-[10px] font-semibold text-amber-700">
                                    needs categorizing
                                  </span>
                                )}
                              </span>
                            </td>
                            <td className="py-2.5 pr-3 hidden sm:table-cell">
                              <span className="flex items-center gap-2">
                                <span className="h-1.5 flex-1 max-w-[140px] rounded-full bg-muted overflow-hidden">
                                  <span
                                    className="block h-full rounded-full"
                                    style={{
                                      width: `${Math.max(2, Math.round(row.share * 100))}%`,
                                      backgroundColor: barColor,
                                    }}
                                  />
                                </span>
                                <span className="text-xs text-muted-foreground tabular-nums w-9">
                                  {row.share < 0.01
                                    ? "<1%"
                                    : `${Math.round(row.share * 100)}%`}
                                </span>
                              </span>
                            </td>
                            <td className="py-2.5 pr-3 text-right font-mono font-semibold text-foreground whitespace-nowrap tabular-nums">
                              {pre}
                              {formatMoney(row.net, row.currency)}
                            </td>
                            <td className="py-2.5 pr-3 text-right">
                              <DeltaCell
                                row={row}
                                compareLabel={compareLabel}
                                approx={approx}
                              />
                            </td>
                            <td className="py-2.5 pr-1 text-right text-muted-foreground tabular-nums">
                              {row.transaction_count}
                            </td>
                            <td className="py-2.5 text-right">
                              <ChevronRight className="h-4 w-4 text-muted-foreground/50" />
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                    );
                  })}
                </table>
              </div>
            </CardContent>
          </Card>
        );
      })}
    </div>
  );
}
