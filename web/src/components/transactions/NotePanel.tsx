import { useState, useEffect, useRef } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { X, Save, Loader2, StickyNote } from "lucide-react";
import { cn } from "@/lib/utils";

interface NotePanelProps {
  open: boolean;
  onClose: () => void;
  transactionId: string | null;
  transactionDescription: string | null;
  currentNote: string | null;
}

export function NotePanel({
  open,
  onClose,
  transactionId,
  transactionDescription,
  currentNote,
}: NotePanelProps) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState(currentNote ?? "");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Sync when the panel opens with a new transaction
  useEffect(() => {
    setNote(currentNote ?? "");
  }, [transactionId, currentNote]);

  // Auto-focus the textarea when panel opens
  useEffect(() => {
    if (open && textareaRef.current) {
      setTimeout(() => textareaRef.current?.focus(), 150);
    }
  }, [open]);

  const saveMutation = useMutation({
    mutationFn: (noteText: string) =>
      api.patch(`/api/transactions/${transactionId}/note`, { note: noteText }),
    onSuccess: (_data, noteText) => {
      toast.success(noteText.trim() ? "Note saved." : "Note cleared.");
      queryClient.invalidateQueries({ queryKey: ["transactions"] });
      onClose();
    },
    onError: () => {
      toast.error("Couldn't save note — please try again.");
    },
  });

  if (!open) return null;

  return (
    <div className="fixed inset-y-0 right-0 z-50 w-[380px] bg-card border-l border-border shadow-2xl flex flex-col animate-in slide-in-from-right-full duration-200">
      {/* Header */}
      <div className="flex items-center justify-between px-5 py-4 border-b border-border/60">
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="flex-shrink-0 h-8 w-8 rounded-lg bg-primary/10 flex items-center justify-center">
            <StickyNote className="h-4 w-4 text-primary" />
          </div>
          <div className="min-w-0">
            <h3 className="text-sm font-semibold text-foreground">Transaction Note</h3>
            <p className="text-[11px] text-muted-foreground truncate max-w-[240px]">
              #{transactionId} {transactionDescription ? `\u00B7 ${transactionDescription}` : ""}
            </p>
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 rounded-full hover:bg-muted"
          onClick={onClose}
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Note input */}
      <div className="flex-1 p-5">
        <textarea
          ref={textareaRef}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          placeholder="Add a note about this transaction..."
          className={cn(
            "w-full h-full min-h-[200px] resize-none rounded-xl border border-border/60 bg-background p-4",
            "text-sm leading-relaxed placeholder:text-muted-foreground/50",
            "focus:outline-none focus:ring-2 focus:ring-primary/30 focus:border-primary/40",
            "transition-colors"
          )}
        />
      </div>

      {/* Footer */}
      <div className="flex items-center gap-2.5 px-5 py-4 border-t border-border/60 bg-muted/20">
        <Button
          variant="outline"
          className="flex-1 rounded-lg"
          onClick={onClose}
        >
          Cancel
        </Button>
        <Button
          className="flex-1 bg-primary hover:bg-primary/90 text-primary-foreground rounded-lg shadow-warm-sm"
          onClick={() => saveMutation.mutate(note)}
          disabled={saveMutation.isPending}
        >
          {saveMutation.isPending ? (
            <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
          ) : (
            <Save className="h-4 w-4 mr-1.5" />
          )}
          Save Note
        </Button>
      </div>
    </div>
  );
}
