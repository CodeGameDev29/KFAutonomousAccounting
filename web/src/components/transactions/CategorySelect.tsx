import { useMemo } from "react";
import { SECTION_META } from "@/types/category-taxonomy";
import type { CategoryGroup } from "@/types/category-taxonomy";

interface CategorySelectProps {
  /** Current category (null = uncategorized). */
  value: string | null;
  /** Fixed taxonomy from useCategoryTaxonomy().groups. */
  groups: CategoryGroup[];
  /** Fires only when the picked value differs from `value`. */
  onChange: (next: string) => void;
  /** Disabled first option shown when `value` is null. */
  placeholder?: string;
  /** Passed through to the <select>. */
  className?: string;
  /** aria-label for the select. */
  ariaLabel?: string;
  /** Native tooltip. Defaults to `value ?? placeholder`. */
  title?: string;
  /** When true, onClick calls e.stopPropagation() (needed inside clickable table rows). */
  stopPropagation?: boolean;
}

/**
 * One presentational native <select> that renders <optgroup>s from the fixed
 * ISED/GIFI taxonomy and centralizes placeholder / journal-only / legacy-value
 * logic. Shared by the inline table cell, the bulk toolbar, and the slide-over.
 */
export function CategorySelect({
  value,
  groups,
  onChange,
  placeholder = "Uncategorized",
  className,
  ariaLabel,
  title,
  stopPropagation = false,
}: CategorySelectProps) {
  // Visible groups: exclude journal-only / kind==="none" options and the
  // uncategorized pseudo-section, then order by SECTION_META.order.
  const visibleGroups = useMemo(
    () =>
      groups
        .filter((g) => g.section !== "uncategorized")
        .map((g) => ({
          ...g,
          options: g.options.filter(
            (o) => o.journal_only !== true && o.kind !== "none",
          ),
        }))
        .filter((g) => g.options.length > 0)
        .sort(
          (a, b) => SECTION_META[a.section].order - SECTION_META[b.section].order,
        ),
    [groups],
  );

  // Legacy-value guard: a stored value not present in any visible group must
  // still render (never silently blank a stored category).
  const isLegacy = useMemo(() => {
    if (value == null || value === "") return false;
    for (const g of visibleGroups) {
      for (const o of g.options) if (o.name === value) return false;
    }
    return true;
  }, [visibleGroups, value]);

  return (
    <select
      value={value ?? ""}
      onChange={(e) => {
        const next = e.target.value;
        if (next && next !== value) onChange(next);
      }}
      onClick={stopPropagation ? (e) => e.stopPropagation() : undefined}
      title={title ?? value ?? placeholder}
      aria-label={ariaLabel}
      className={className}
    >
      <option value="" disabled>
        {value ?? placeholder}
      </option>
      {visibleGroups.map((g) => (
        <optgroup key={g.section} label={SECTION_META[g.section].pickerLabel}>
          {g.options.map((o) => (
            <option key={o.name} value={o.name}>
              {o.name}
            </option>
          ))}
        </optgroup>
      ))}
      {isLegacy && value != null && (
        <option value={value} disabled>
          {value} (legacy)
        </option>
      )}
    </select>
  );
}
