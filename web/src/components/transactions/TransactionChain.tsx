import { ArrowRight, FileCheck, Wallet, CreditCard, Globe, DollarSign } from "lucide-react";
import { cn } from "@/lib/utils";
import { useTransactionChain } from "@/hooks/use-transaction-links";
import type { ChainNode } from "@/types/transaction-links";

interface TransactionChainProps {
  transactionId: number;
  onNavigate: (txnId: number, sourceFile: string) => void;
}

function AccountIcon({ account }: { account: string }) {
  switch (account) {
    case "CreditCard":
      return <CreditCard className="h-3.5 w-3.5" />;
    case "Wise":
      return <Globe className="h-3.5 w-3.5" />;
    case "USD":
      return <DollarSign className="h-3.5 w-3.5" />;
    default:
      return <Wallet className="h-3.5 w-3.5" />;
  }
}

function formatNodeAmount(node: ChainNode): string {
  const amt = Math.abs(parseFloat(node.amount));
  return `$${amt.toLocaleString("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 })} ${node.currency}`;
}

export function TransactionChain({ transactionId, onNavigate }: TransactionChainProps) {
  const { data: chainData, isLoading } = useTransactionChain(transactionId);

  if (isLoading || !chainData || chainData.nodes.length <= 1) {
    return null;
  }

  const { nodes, current_index } = chainData;

  return (
    <div className="space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Transfer Chain
      </h4>
      <div className="flex items-center gap-1 overflow-x-auto pb-1">
        {nodes.map((node, i) => {
          const isCurrent = i === current_index;
          return (
            <div key={node.transaction_id} className="flex items-center gap-1 flex-shrink-0">
              {i > 0 && (
                <ArrowRight className="h-3.5 w-3.5 text-muted-foreground/50 flex-shrink-0" />
              )}
              <button
                onClick={() => {
                  if (!isCurrent) {
                    onNavigate(node.transaction_id, node.source_file);
                  }
                }}
                disabled={isCurrent}
                className={cn(
                  "flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium transition-all",
                  isCurrent
                    ? "bg-primary/10 text-primary ring-2 ring-primary/30 cursor-default"
                    : "bg-muted/50 text-muted-foreground hover:bg-primary/5 hover:text-primary cursor-pointer"
                )}
                title={node.description}
              >
                <AccountIcon account={node.account} />
                <span className="whitespace-nowrap">{formatNodeAmount(node)}</span>
                {node.has_document_match && (
                  <FileCheck className="h-3 w-3 text-green-600 dark:text-green-400" />
                )}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}
