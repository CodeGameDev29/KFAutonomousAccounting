import { ArrowLeftRight } from "lucide-react";
import { useNavigate } from "react-router-dom";
import type { LinkedTransactionInfo } from "@/types/transaction-links";

interface TransactionLinkBadgeProps {
  link: LinkedTransactionInfo;
}

function formatLinkLabel(link: LinkedTransactionInfo): string {
  const amount = Math.abs(parseFloat(link.linked_amount)).toLocaleString("en-CA", {
    style: "currency",
    currency: link.linked_currency === "CAD" || link.linked_currency === "USD" ? link.linked_currency : "CAD",
    minimumFractionDigits: 2,
  });
  return `→ ${link.linked_account}: ${amount}`;
}

export function TransactionLinkBadge({ link }: TransactionLinkBadgeProps) {
  const navigate = useNavigate();

  const handleClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    const params = new URLSearchParams();
    params.set("statement", link.linked_source_file);
    params.set("highlight", String(link.linked_txn_id));
    navigate(`/transactions?${params.toString()}`);
  };

  return (
    <button
      onClick={handleClick}
      className="text-sm text-indigo-600 dark:text-indigo-400 hover:underline truncate max-w-[180px] flex items-center gap-1 transition-colors"
      title={`${link.link_type}: ${link.linked_description}`}
      aria-label={`Navigate to linked transaction in ${link.linked_account}`}
    >
      <ArrowLeftRight className="h-3.5 w-3.5 flex-shrink-0 text-indigo-500" />
      <span className="truncate">{formatLinkLabel(link)}</span>
    </button>
  );
}
