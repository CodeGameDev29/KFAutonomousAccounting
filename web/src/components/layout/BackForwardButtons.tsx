import { useEffect, useState } from "react";
import { useLocation, useNavigate, useNavigationType } from "react-router-dom";
import { ArrowLeft, ArrowRight } from "lucide-react";
import { Button } from "@/components/ui/button";

const MAX_IDX_KEY = "aa_nav_max_idx";

/**
 * Back/forward availability derived from React Router's history index
 * (window.history.state.idx). Works with the same history stack the
 * browser's own back/forward buttons use, so the two stay in sync.
 */
function useNavHistory() {
  const location = useLocation();
  const navType = useNavigationType();
  const idx = (window.history.state?.idx as number | undefined) ?? 0;
  const [maxIdx, setMaxIdx] = useState<number>(() => {
    const stored = Number(sessionStorage.getItem(MAX_IDX_KEY));
    return Number.isFinite(stored) ? stored : 0;
  });

  useEffect(() => {
    // A PUSH after going back truncates the forward stack; REPLACE keeps
    // the same index; POP just moves within the stack.
    const next =
      navType === "PUSH"
        ? idx
        : Math.max(Number(sessionStorage.getItem(MAX_IDX_KEY)) || 0, idx);
    sessionStorage.setItem(MAX_IDX_KEY, String(next));
    setMaxIdx(next);
  }, [location, navType, idx]);

  return { canGoBack: idx > 0, canGoForward: idx < maxIdx };
}

export function BackForwardButtons() {
  const navigate = useNavigate();
  const { canGoBack, canGoForward } = useNavHistory();

  return (
    <div className="flex items-center gap-0.5">
      <Button
        variant="ghost"
        size="sm"
        className="h-7 w-7 p-0"
        disabled={!canGoBack}
        onClick={() => navigate(-1)}
        aria-label="Back"
        title="Back"
      >
        <ArrowLeft className="h-4 w-4" />
      </Button>
      <Button
        variant="ghost"
        size="sm"
        className="h-7 w-7 p-0"
        disabled={!canGoForward}
        onClick={() => navigate(1)}
        aria-label="Forward"
        title="Forward"
      >
        <ArrowRight className="h-4 w-4" />
      </Button>
    </div>
  );
}
