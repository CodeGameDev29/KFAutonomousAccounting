import {
  addDays,
  addMonths,
  differenceInCalendarDays,
  endOfMonth,
  format,
  parseISO,
  startOfMonth,
  subMonths,
} from "date-fns";

export interface Period {
  start: string;
  end: string;
}

export function toIso(d: Date): string {
  return format(d, "yyyy-MM-dd");
}

/** True when [start, end] is exactly one calendar month. */
export function isCalendarMonth(start: string, end: string): boolean {
  try {
    const s = parseISO(start);
    const e = parseISO(end);
    return (
      s.getDate() === 1 &&
      s.getFullYear() === e.getFullYear() &&
      s.getMonth() === e.getMonth() &&
      toIso(endOfMonth(s)) === end
    );
  } catch {
    return false;
  }
}

export function monthPeriod(d: Date): Period {
  return { start: toIso(startOfMonth(d)), end: toIso(endOfMonth(d)) };
}

/**
 * The comparison window immediately before [start, end]: the previous
 * calendar month when the range is a calendar month, otherwise the
 * same-length window ending the day before `start`.
 */
export function previousPeriod(start: string, end: string): Period {
  if (isCalendarMonth(start, end)) {
    return monthPeriod(subMonths(parseISO(start), 1));
  }
  const s = parseISO(start);
  const e = parseISO(end);
  const days = differenceInCalendarDays(e, s) + 1;
  const prevEnd = addDays(s, -1);
  return { start: toIso(addDays(prevEnd, -(days - 1))), end: toIso(prevEnd) };
}

export function nextMonthPeriod(start: string): Period {
  return monthPeriod(addMonths(parseISO(start), 1));
}

export function prevMonthPeriod(start: string): Period {
  return monthPeriod(subMonths(parseISO(start), 1));
}

/** "May 2026" for a calendar month, "May 1 – May 31, 2026" otherwise. */
export function periodLabel(start: string, end: string): string {
  try {
    if (isCalendarMonth(start, end)) {
      return format(parseISO(start), "MMMM yyyy");
    }
    return `${format(parseISO(start), "MMM d, yyyy")} – ${format(parseISO(end), "MMM d, yyyy")}`;
  } catch {
    return "";
  }
}

/** Short name for the comparison window: "Apr" for a month, "prior period" otherwise. */
export function comparisonLabel(start: string, end: string): string {
  if (isCalendarMonth(start, end)) {
    return format(subMonths(parseISO(start), 1), "MMM");
  }
  return "prior period";
}
