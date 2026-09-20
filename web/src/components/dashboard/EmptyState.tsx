import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { CheckCircle2, ArrowRight, Upload, Landmark, RefreshCw } from "lucide-react";

/** Simple indigo-colored illustration for empty states — inspired by unDraw */
function EmptyIllustration() {
  return (
    <svg
      width="120"
      height="120"
      viewBox="0 0 120 120"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className="mx-auto mb-2"
    >
      {/* Stack of documents */}
      <rect x="30" y="35" width="50" height="60" rx="4" fill="hsl(var(--indigo-50))" stroke="hsl(var(--primary))" strokeWidth="1.5" />
      <rect x="35" y="30" width="50" height="60" rx="4" fill="hsl(var(--indigo-50))" stroke="hsl(var(--primary))" strokeWidth="1.5" />
      <rect x="40" y="25" width="50" height="60" rx="4" fill="white" stroke="hsl(var(--primary))" strokeWidth="1.5" />
      {/* Lines on top document */}
      <line x1="48" y1="40" x2="82" y2="40" stroke="hsl(var(--primary))" strokeWidth="1.5" strokeLinecap="round" opacity="0.4" />
      <line x1="48" y1="48" x2="78" y2="48" stroke="hsl(var(--primary))" strokeWidth="1.5" strokeLinecap="round" opacity="0.3" />
      <line x1="48" y1="56" x2="74" y2="56" stroke="hsl(var(--primary))" strokeWidth="1.5" strokeLinecap="round" opacity="0.2" />
      {/* Checkmark circle */}
      <circle cx="82" cy="78" r="14" fill="hsl(var(--primary))" />
      <path d="M76 78L80 82L88 74" stroke="white" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function EmptyState() {
  const navigate = useNavigate();

  const { data: summary } = useQuery<{ document_count?: number; total_transactions?: number }>({
    queryKey: ["dashboard", "summary"],
    queryFn: () => api.get("/api/dashboard/summary"),
  });

  const hasReceipts = (summary?.document_count ?? 0) > 0;
  const hasTransactions = (summary?.total_transactions ?? 0) > 0;

  const items = [
    {
      label: "Upload your first receipt",
      description: "Drag and drop a PDF or image",
      icon: Upload,
      done: hasReceipts,
      action: () => navigate("/transactions"),
    },
    {
      label: "Upload your bank statement",
      description: "Download a PDF or CSV from your bank. Drop it here.",
      icon: Landmark,
      done: hasTransactions,
      action: () => navigate("/transactions"),
    },
    {
      label: "Run your first reconciliation",
      description: "Match transactions to receipts intelligently",
      icon: RefreshCw,
      done: false,
      action: () => navigate("/transactions"),
      disabled: !hasReceipts || !hasTransactions,
    },
  ];

  return (
    <Card className="shadow-warm rounded-xl">
      <CardHeader className="text-center pb-2">
        <EmptyIllustration />
        <CardTitle className="text-lg font-bold">Get started with your books</CardTitle>
        <p className="text-sm text-muted-foreground">
          Three quick steps to automate your bookkeeping.
        </p>
      </CardHeader>
      <CardContent>
        <ul className="space-y-3">
          {items.map((item, index) => (
            <li
              key={item.label}
              className="flex items-center justify-between rounded-xl border p-3.5 shadow-warm-sm transition-all hover:shadow-warm-md hover:-translate-y-0.5"
            >
              <div className="flex items-center gap-3">
                <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-muted text-muted-foreground text-sm font-semibold">
                  {item.done ? (
                    <CheckCircle2 className="h-5 w-5 text-success" />
                  ) : (
                    <span>{index + 1}</span>
                  )}
                </div>
                <div>
                  <span
                    className={
                      item.done
                        ? "text-sm text-muted-foreground line-through"
                        : "text-sm font-medium"
                    }
                  >
                    {item.label}
                  </span>
                  {!item.done && (
                    <p className="text-xs text-muted-foreground">{item.description}</p>
                  )}
                </div>
              </div>
              {!item.done && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={item.action}
                  disabled={item.disabled}
                  className="text-primary hover:text-primary hover:bg-primary/5 gap-1"
                >
                  Start
                  <ArrowRight className="h-3.5 w-3.5" />
                </Button>
              )}
            </li>
          ))}
        </ul>
      </CardContent>
    </Card>
  );
}
