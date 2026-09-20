import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { api } from "@/lib/api";
import type { CategoriesResponse, CategoryGroup, IsedSection } from "@/types/category-taxonomy";

export interface CategoryTaxonomy {
  /** Flat list, section-ordered (back-compat with existing `categories` prop). */
  flat: string[];
  /** Grouped taxonomy for optgroups + analytics grouping. */
  groups: CategoryGroup[];
  /** name -> ISED section; unknown/legacy names resolve to "uncategorized". */
  sectionOf: (name: string | null | undefined) => IsedSection;
  isLoading: boolean;
}

export function useCategoryTaxonomy(): CategoryTaxonomy {
  const { data, isLoading } = useQuery<CategoriesResponse>({
    queryKey: ["expense-categories"],
    queryFn: () => api.get("/api/transactions/categories"),
    staleTime: 60 * 60 * 1000, // 1h — the fixed taxonomy rarely changes
  });

  const groups = data?.groups ?? [];
  const flat = data?.categories ?? [];

  const sectionMap = useMemo(() => {
    const m = new Map<string, IsedSection>();
    for (const g of groups) for (const o of g.options) m.set(o.name, o.section);
    return m;
  }, [groups]);

  const sectionOf = (name: string | null | undefined): IsedSection =>
    (name && sectionMap.get(name)) || "uncategorized";

  return { flat, groups, sectionOf, isLoading };
}
