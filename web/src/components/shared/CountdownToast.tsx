import { useState, useEffect, useRef, useCallback } from "react";
import { Button } from "@/components/ui/button";

interface CountdownToastProps {
  message: string;
  duration?: number;         // ms, default 12000
  actionLabel?: string;      // default "Undo"
  onAction: () => void;
  onExpire: () => void;
}

export function CountdownToast({
  message,
  duration = 12000,
  actionLabel = "Undo",
  onAction,
  onExpire,
}: CountdownToastProps) {
  const [progress, setProgress] = useState(100);
  const startRef = useRef(Date.now());
  const rafRef = useRef<number>(undefined);
  const timerRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  const tick = useCallback(() => {
    const elapsed = Date.now() - startRef.current;
    const remaining = Math.max(0, 100 - (elapsed / duration) * 100);
    setProgress(remaining);
    if (remaining > 0) {
      rafRef.current = requestAnimationFrame(tick);
    }
  }, [duration]);

  useEffect(() => {
    rafRef.current = requestAnimationFrame(tick);
    timerRef.current = setTimeout(onExpire, duration);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [tick, onExpire, duration]);

  return (
    <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 flex flex-col rounded-xl
      border border-border/60 bg-card shadow-warm-md animate-in slide-in-from-bottom-4
      fade-in duration-200 overflow-hidden">
      <div className="flex items-center gap-3 px-4 py-3">
        <span className="text-sm text-foreground">{message}</span>
        <Button variant="outline" size="sm"
          className="h-7 text-xs border-primary/30 text-primary hover:bg-primary/5"
          onClick={onAction}>
          {actionLabel}
        </Button>
      </div>
      <div className="h-1 w-full bg-muted">
        <div className="h-full bg-primary/40" style={{ width: `${progress}%` }} />
      </div>
    </div>
  );
}
