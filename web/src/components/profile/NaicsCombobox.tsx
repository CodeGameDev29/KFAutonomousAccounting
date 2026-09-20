import { useMemo, useState } from "react";
import { Check, ChevronsUpDown, Search } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { NAICS_OPTIONS } from "@/lib/naics";
import { cn } from "@/lib/utils";

export interface NaicsComboboxProps {
  /** Current naics_code ("" = unset). */
  value: string;
  /** Called with the chosen code; "" clears the selection. */
  onChange: (code: string) => void;
  id?: string;
}

/**
 * Searchable NAICS industry-code picker.
 *
 * Built on the existing Radix `Popover` + `Input` primitives (the repo does not
 * depend on `cmdk`, so shadcn's `Command` is unavailable). Filters the curated
 * `NAICS_OPTIONS` by label OR code substring, offers a "skip" item, and lets the
 * user commit any raw 2–6 digit code that isn't in the shortlist so precision
 * isn't capped by the curated list. Keyboard-accessible: the search input
 * autofocuses, items are real <button>s (Tab-navigable), Escape closes.
 */
export function NaicsCombobox({ value, onChange, id }: NaicsComboboxProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  const selected = useMemo(
    () => NAICS_OPTIONS.find((o) => o.code === value),
    [value]
  );

  const q = query.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!q) return NAICS_OPTIONS;
    return NAICS_OPTIONS.filter(
      (o) => o.label.toLowerCase().includes(q) || o.code.includes(q)
    );
  }, [q]);

  // Free entry: a raw 2–6 digit code not already in the curated list.
  const rawCode = query.trim();
  const rawNotInList =
    /^\d{2,6}$/.test(rawCode) && !NAICS_OPTIONS.some((o) => o.code === rawCode);

  const triggerLabel = selected
    ? `${selected.label} (${selected.code})`
    : value
      ? `Code ${value}`
      : "Search your industry (NAICS)…";

  function commit(code: string) {
    onChange(code);
    setQuery("");
    setOpen(false);
  }

  const itemClass =
    "flex w-full items-center gap-2 rounded-sm px-2 py-2 text-left text-sm hover:bg-accent hover:text-accent-foreground focus-visible:bg-accent focus-visible:outline-none";

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) setQuery("");
      }}
    >
      <PopoverTrigger asChild>
        <Button
          id={id}
          type="button"
          variant="outline"
          role="combobox"
          aria-expanded={open}
          className={cn(
            "w-full justify-between font-normal focus-visible:ring-primary",
            !selected && !value && "text-muted-foreground"
          )}
        >
          <span className="truncate">{triggerLabel}</span>
          <ChevronsUpDown className="ml-2 h-4 w-4 shrink-0 opacity-50" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-[var(--radix-popover-trigger-width)] p-0"
      >
        <div className="flex items-center border-b px-3">
          <Search className="mr-2 h-4 w-4 shrink-0 opacity-50" />
          <Input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search by name or code…"
            aria-label="Search NAICS industry codes"
            className="h-10 border-0 px-0 focus-visible:ring-0 focus-visible:ring-offset-0"
          />
        </div>
        <div className="max-h-64 overflow-y-auto p-1" role="listbox">
          <button
            type="button"
            role="option"
            aria-selected={value === ""}
            onClick={() => commit("")}
            className={itemClass}
          >
            <Check
              className={cn(
                "h-4 w-4 shrink-0",
                value === "" ? "opacity-100" : "opacity-0"
              )}
            />
            <span className="text-muted-foreground">Not sure — skip for now</span>
          </button>

          {rawNotInList && (
            <button
              type="button"
              role="option"
              aria-selected={value === rawCode}
              onClick={() => commit(rawCode)}
              className={itemClass}
            >
              <Check
                className={cn(
                  "h-4 w-4 shrink-0",
                  value === rawCode ? "opacity-100" : "opacity-0"
                )}
              />
              <span className="flex-1 truncate">
                Use code <span className="font-mono">{rawCode}</span>
              </span>
            </button>
          )}

          {filtered.map((opt) => (
            <button
              key={opt.code}
              type="button"
              role="option"
              aria-selected={value === opt.code}
              onClick={() => commit(opt.code)}
              className={itemClass}
            >
              <Check
                className={cn(
                  "h-4 w-4 shrink-0",
                  value === opt.code ? "opacity-100" : "opacity-0"
                )}
              />
              <span className="flex-1 truncate">{opt.label}</span>
              <span className="ml-2 shrink-0 font-mono text-xs text-muted-foreground">
                {opt.code}
              </span>
            </button>
          ))}

          {filtered.length === 0 && !rawNotInList && (
            <p className="px-2 py-6 text-center text-sm text-muted-foreground">
              No match — you can skip this.
            </p>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
