import { Fragment } from "react";
import { ChevronRight } from "lucide-react";

export interface BreadcrumbSegment {
  label: string;
  onClick?: () => void;
}

interface BreadcrumbsProps {
  segments: BreadcrumbSegment[];
}

export function Breadcrumbs({ segments }: BreadcrumbsProps) {
  return (
    <nav className="flex items-center gap-1 text-sm" aria-label="Breadcrumb">
      {segments.map((seg, i) => {
        const isLast = i === segments.length - 1;
        return (
          <Fragment key={i}>
            {i > 0 && <ChevronRight className="h-3.5 w-3.5 text-muted-foreground/50" aria-hidden />}
            {isLast || !seg.onClick ? (
              <span className="font-medium text-foreground">{seg.label}</span>
            ) : (
              <button onClick={seg.onClick}
                className="text-muted-foreground hover:text-foreground transition-colors">
                {seg.label}
              </button>
            )}
          </Fragment>
        );
      })}
    </nav>
  );
}
