import { useMemo, useState } from "react";
import {
  startOfMonth,
  endOfMonth,
  startOfYear,
  subMonths,
  subDays,
  parseISO,
  format,
} from "date-fns";
import { monthPeriod, periodLabel, toIso } from "@/lib/period";
import { CalendarIcon, Check } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { cn } from "@/lib/utils";

export interface DateRangeValue {
  start: string;
  end: string;
}

interface DateRangePickerProps {
  value: DateRangeValue | null;
  onChange: (next: DateRangeValue) => void;
  /**
   * Latest date with any data (ISO). When it's in the past, presets anchor
   * to it instead of today, so "This Month" / "Last 30 Days" always land on
   * the most recent period that actually has transactions.
   */
  dataThrough?: string | null;
}

type PresetKey = "this-month" | "last-month" | "last-30" | "ytd";

/** How many months the month picker offers, newest first. */
const MONTH_CHOICES = 20;

function presetAnchor(dataThrough?: string | null): Date {
  const now = new Date();
  if (!dataThrough) return now;
  try {
    const latest = parseISO(dataThrough);
    return latest < now ? latest : now;
  } catch {
    return now;
  }
}

function computePreset(key: PresetKey, anchor: Date): DateRangeValue {
  switch (key) {
    case "this-month":
      return {
        start: toIso(startOfMonth(anchor)),
        end: toIso(endOfMonth(anchor)),
      };
    case "last-month": {
      const prev = subMonths(anchor, 1);
      return { start: toIso(startOfMonth(prev)), end: toIso(endOfMonth(prev)) };
    }
    case "last-30":
      return { start: toIso(subDays(anchor, 29)), end: toIso(anchor) };
    case "ytd":
      return { start: toIso(startOfYear(anchor)), end: toIso(anchor) };
  }
}

function matchesPreset(
  value: DateRangeValue | null,
  key: PresetKey,
  anchor: Date,
): boolean {
  if (!value) return false;
  const preset = computePreset(key, anchor);
  return preset.start === value.start && preset.end === value.end;
}

function formatRangeLabel(value: DateRangeValue | null): string {
  if (!value) return "Pick your own month";
  return periodLabel(value.start, value.end) || "Pick your own month";
}

interface MonthChoice {
  key: string;
  label: string;
  period: DateRangeValue;
}

/**
 * The last {@link MONTH_CHOICES} months, newest first, each as a whole
 * calendar month (1st → last day).
 *
 * Anchored on the same date the presets use — today, or the newest month that
 * actually has transactions when that is in the past. Deliberately NOT
 * anchored on whatever month is currently on screen: that list would slide
 * every time you picked from it, and once you stepped back a year there would
 * be no way to walk forward again.
 */
function monthChoices(anchor: Date): MonthChoice[] {
  return Array.from({ length: MONTH_CHOICES }, (_, i) => {
    const month = startOfMonth(subMonths(anchor, i));
    return {
      key: format(month, "yyyy-MM"),
      label: format(month, "MMMM yyyy"),
      period: monthPeriod(month),
    };
  });
}

const PRESETS: { key: PresetKey; label: string }[] = [
  { key: "this-month", label: "This Month" },
  { key: "last-month", label: "Last Month" },
  { key: "last-30", label: "Last 30 Days" },
  { key: "ytd", label: "YTD" },
];

export function DateRangePicker({
  value,
  onChange,
  dataThrough,
}: DateRangePickerProps) {
  const [popoverOpen, setPopoverOpen] = useState(false);

  const anchor = useMemo(() => presetAnchor(dataThrough), [dataThrough]);
  const months = useMemo(() => monthChoices(anchor), [anchor]);

  const activePreset = useMemo<PresetKey | null>(() => {
    for (const p of PRESETS) {
      if (matchesPreset(value, p.key, anchor)) return p.key;
    }
    return null;
  }, [value, anchor]);

  const handlePreset = (key: PresetKey) => {
    const next = computePreset(key, anchor);
    if (value && value.start === next.start && value.end === next.end) return;
    onChange(next);
  };

  const handleMonth = (choice: MonthChoice) => {
    setPopoverOpen(false);
    if (
      value &&
      value.start === choice.period.start &&
      value.end === choice.period.end
    ) {
      return;
    }
    onChange(choice.period);
  };

  const monthPicked = activePreset === null && value !== null;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {PRESETS.map((p) => {
        const isActive = activePreset === p.key;
        return (
          <Button
            key={p.key}
            variant={isActive ? "default" : "outline"}
            size="sm"
            onClick={() => handlePreset(p.key)}
            className={cn(
              "h-8",
              isActive && "bg-primary hover:bg-primary-hover text-white",
            )}
          >
            {p.label}
          </Button>
        );
      })}

      <Popover open={popoverOpen} onOpenChange={setPopoverOpen}>
        <PopoverTrigger asChild>
          <Button
            variant={monthPicked ? "default" : "outline"}
            size="sm"
            className={cn(
              "h-8 gap-2",
              monthPicked && "bg-primary hover:bg-primary-hover text-white",
            )}
          >
            <CalendarIcon className="h-3.5 w-3.5" />
            {monthPicked ? formatRangeLabel(value) : "Pick your own month"}
          </Button>
        </PopoverTrigger>
        <PopoverContent className="w-[20rem] p-0" align="end">
          <p className="border-b border-border/60 px-3 py-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
            Pick your own month
          </p>
          <div
            className="grid max-h-[19rem] grid-cols-2 gap-1 overflow-y-auto p-2"
            role="listbox"
            aria-label="Pick your own month"
          >
            {months.map((m) => {
              const isSelected =
                value?.start === m.period.start && value?.end === m.period.end;
              return (
                <button
                  key={m.key}
                  role="option"
                  aria-selected={isSelected}
                  onClick={() => handleMonth(m)}
                  className={cn(
                    "flex items-center justify-between gap-1 rounded-md px-2.5 py-2 text-left text-sm transition-colors",
                    isSelected
                      ? "bg-primary text-white"
                      : "hover:bg-primary/5 hover:text-primary",
                  )}
                >
                  <span className="truncate">{m.label}</span>
                  {isSelected && <Check className="h-3.5 w-3.5 shrink-0" />}
                </button>
              );
            })}
          </div>
        </PopoverContent>
      </Popover>
    </div>
  );
}
