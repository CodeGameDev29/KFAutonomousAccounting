import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";

interface Transaction {
  id: string;
  date: string;
  description: string;
  amount: number;
  currency: string;
  account: string;
  type: string;
  status: string;
  has_receipt: boolean;
  source_file: string | null;
}

interface RecentTransactionsResponse {
  transactions: Transaction[];
}

function formatCurrency(amount: number, currency: string): string {
  const absAmount = Math.abs(amount);
  return absAmount.toLocaleString("en-CA", {
    style: "currency",
    currency: currency || "CAD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function formatDate(dateString: string): string {
  // A bare YYYY-MM-DD parses as UTC midnight; pin it to local midnight.
  const date = new Date(`${dateString}T00:00:00`);
  return date.toLocaleDateString("en-CA", {
    month: "short",
    day: "numeric",
  });
}

export function RecentTransactions() {
  const navigate = useNavigate();
  const { data, isLoading } = useQuery<RecentTransactionsResponse>({
    queryKey: ["dashboard", "recent-transactions"],
    queryFn: () => api.get("/api/dashboard/recent-transactions?limit=10"),
  });

  return (
    <Card className="shadow-warm rounded-xl">
      <CardHeader>
        <CardTitle className="text-lg font-bold">Recent Transactions</CardTitle>
        <CardDescription>Your latest bank and receipt activity</CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading || !data ? (
          <div className="space-y-4">
            {Array.from({ length: 5 }).map((_, i) => (
              <div key={i} className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                  <Skeleton className="h-4 w-12" />
                  <Skeleton className="h-4 w-32" />
                </div>
                <Skeleton className="h-4 w-20" />
              </div>
            ))}
          </div>
        ) : data.transactions.length === 0 ? (
          <p className="py-8 text-center text-sm text-muted-foreground">
            No transactions yet. Upload a bank statement to see activity here.
          </p>
        ) : (
          <div className="space-y-1">
            {data.transactions.map((txn) => {
              const isCredit = txn.type ? txn.type === "CREDIT" : txn.amount > 0;
              return (
                <div
                  key={txn.id}
                  className="flex items-center justify-between rounded-lg px-2 py-2.5 transition-colors hover:bg-muted/30 cursor-pointer"
                  onClick={() => {
                    const params = new URLSearchParams();
                    if (txn.source_file) {
                      params.set("statement", txn.source_file);
                    }
                    params.set("highlight", String(txn.id));
                    navigate(`/transactions?${params.toString()}`);
                  }}
                >
                  <div className="flex items-center gap-3 min-w-0">
                    <span className="w-14 shrink-0 text-xs text-muted-foreground font-mono">
                      {formatDate(txn.date)}
                    </span>
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">
                        {txn.description}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {txn.account}
                      </p>
                    </div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0 ml-4">
                    <span
                      className={`text-sm font-mono font-medium ${
                        isCredit
                          ? "text-primary"
                          : "text-foreground"
                      }`}
                    >
                      {isCredit ? "+" : "-"}
                      {formatCurrency(txn.amount, txn.currency)}
                    </span>
                    {txn.status === "matched" && (
                      <Badge
                        className="text-[10px] px-1.5 py-0 bg-primary/10 text-primary border-primary/20 hover:bg-primary/10"
                      >
                        Matched
                      </Badge>
                    )}
                    {txn.status === "unmatched" && (
                      <Badge
                        variant="outline"
                        className="text-[10px] px-1.5 py-0"
                      >
                        Unmatched
                      </Badge>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
