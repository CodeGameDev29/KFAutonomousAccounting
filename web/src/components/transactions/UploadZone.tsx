import { useRef, useState, useCallback, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import {
  useProcessingQueue,
  type JobKind,
} from "@/stores/processing-queue-store";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Upload, X, FileText, AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";

interface UploadZoneProps {
  open: boolean;
  onClose: () => void;
  /** DOM id on the zone's root, so a caller that just opened it can scroll it
   *  into view (the sidebar's "Upload Documents" does). */
  id?: string;
  /** "auto" (default): CSV/XLSX→statement, and every PDF is sniffed — one that
   *    reads as a bank statement takes the statement path, everything else is a
   *    receipt. This is the "here is a folder" zone, where nothing has said
   *    what each file is.
   *  "statement": all files→statement endpoint (for bank PDF uploads).
   *  "receipt": all files→receipt endpoint. The user said these are receipts,
   *    so nothing second-guesses them. */
  mode?: "auto" | "statement" | "receipt";
}

interface PreviousUpload {
  id: number;
  filename: string;
  vendor: string | null;
  date: string | null;
  total: number | null;
  currency: string | null;
  confidence: string | null;
  status: string;
  matched_transaction_id: number | null;
  matched_source_file: string | null;
}

const RECEIPT_TYPES = [
  "application/pdf",
  "image/jpeg",
  "image/png",
  "image/gif",
  "image/bmp",
  "image/tiff",
  "image/webp",
  "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", // xlsx
  "application/vnd.ms-excel", // xls
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document", // docx
  "application/msword", // doc
  "text/csv",
];

const ACCEPTED_EXTENSIONS =
  ".pdf,.jpg,.jpeg,.png,.gif,.bmp,.tiff,.tif,.webp,.xlsx,.xls,.csv,.docx,.doc";

const MAX_FILES = 200;

function isStatementFile(file: File): boolean {
  const ext = file.name.toLowerCase().split(".").pop();
  return ext === "csv" || ext === "xlsx" || ext === "xls";
}

function isPdf(file: File): boolean {
  return file.name.toLowerCase().endsWith(".pdf");
}

export function UploadZone({ open, onClose, mode = "auto", id }: UploadZoneProps) {
  const navigate = useNavigate();
  // Files are handed to the processing-queue store, which owns the whole life
  // of a document: bytes up the wire, then the server-side job, then the
  // result. Its panel (ProcessingQueuePanel) renders the per-file state, so
  // this component is now just the intake — a drop target and a browse button.
  const uploadFiles = useProcessingQueue((s) => s.uploadFiles);
  const [isDragging, setIsDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const queuedCount = useProcessingQueue(
    (s) => Object.keys(s.rows).length,
  );

  // Fetch previously uploaded receipts (persisted across navigation)
  const { data: previousUploads } = useQuery<{ documents: PreviousUpload[] }>({
    queryKey: ["receipts", "recent"],
    queryFn: () => api.get("/api/receipts?per_page=20"),
    enabled: open && mode === "receipt",
    staleTime: 30_000,
  });

  // Fetch previously uploaded source files (statements)
  const { data: sourceFilesData } = useQuery<{ source_files: Array<{ source_file: string; account: string; transaction_count: number }> }>({
    queryKey: ["source-files"],
    queryFn: () => api.get("/api/transactions/source-files"),
    enabled: open && mode === "statement",
    staleTime: 30_000,
  });

  const [rejectedMsg, setRejectedMsg] = useState<string | null>(null);

  useEffect(() => {
    if (!open) setRejectedMsg(null);
  }, [open]);

  const addFiles = useCallback(
    async (newFiles: FileList | File[]) => {
      const all = Array.from(newFiles);
      const validFiles = all.filter(
        (f) => RECEIPT_TYPES.includes(f.type) || isStatementFile(f)
      );

      const rejectedCount = all.length - validFiles.length;
      if (rejectedCount > 0) {
        const names = all
          .filter((f) => !RECEIPT_TYPES.includes(f.type) && !isStatementFile(f))
          .map((f) => f.name)
          .slice(0, 3)
          .join(", ");
        setRejectedMsg(
          `Unsupported file type${rejectedCount > 1 ? "s" : ""}: ${names}${rejectedCount > 3 ? "..." : ""}. Supported: PDF, JPG, PNG, GIF, BMP, TIFF, WEBP, XLSX, CSV, DOCX.`
        );
      } else {
        setRejectedMsg(null);
      }

      if (validFiles.length === 0) return;

      // Enforce maximum file count per batch (not aggregate across batches)
      const filesToAdd = validFiles.slice(0, MAX_FILES);
      if (filesToAdd.length < validFiles.length) {
        setRejectedMsg(
          `Only ${filesToAdd.length} of ${validFiles.length} files added — maximum ${MAX_FILES} per batch.`
        );
      }

      uploadFiles(
        filesToAdd.map((file) => {
          const asStatement =
            mode === "statement" ||
            (mode === "auto" && isStatementFile(file));
          const kind: JobKind = asStatement ? "statement" : "receipt";
          // In the auto zone nobody has said what a PDF is, so the server
          // reads its text layer before queueing it and routes a bank
          // statement to the statement pipeline instead of extracting a
          // month of transactions as if it were one receipt. The row says
          // "Detected as a bank statement" and offers one click to undo it.
          const sniff = mode === "auto" && !asStatement && isPdf(file);
          return {
            file,
            kind,
            endpoint: asStatement
              ? "/api/transactions/upload-statement"
              : sniff
                ? "/api/receipts/upload?auto_detect=1"
                : "/api/receipts/upload",
          };
        }),
      );
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode]
  );

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(true);
  }

  function handleDragLeave(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(false);
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files.length > 0) {
      void addFiles(e.dataTransfer.files);
    }
  }

  function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files && e.target.files.length > 0) {
      void addFiles(e.target.files);
      e.target.value = "";
    }
  }

  if (!open) return null;

  const prevDocs = previousUploads?.documents ?? [];
  const prevStatements = sourceFilesData?.source_files ?? [];

  return (
    <div
      id={id}
      className="rounded-xl border border-border/60 bg-card p-5 space-y-4 shadow-warm"
    >
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-foreground">
          {mode === "statement" ? "Upload Statements" : mode === "receipt" ? "Upload Receipts & Invoices" : "Upload Documents"}
        </h3>
        <Button
          variant="ghost"
          size="icon"
          onClick={onClose}
          className="h-7 w-7 rounded-full hover:bg-muted"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>

      {/* Hidden file input — triggered by the Browse button below via
          fileInputRef.current.click(). Uses display:none ("hidden") which
          is the canonical pattern that reliably opens the OS file picker
          when .click() is called from a trusted user gesture. Clip-based
          hiding (sr-only) can leave the picker unopened in some browsers. */}
      {/* No `capture` attribute: on phones it would force the camera open;
          without it iOS/Android show the chooser (photo library / camera /
          files) so users can pick existing photos. */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        accept={ACCEPTED_EXTENSIONS}
        onChange={handleFileSelect}
        className="hidden"
      />

      {/* Drop Zone — pure drag-and-drop target. No click handler, no label. */}
      <div
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className={cn(
          "border-2 border-dashed rounded-xl text-center transition-all duration-200",
          queuedCount > 0 ? "p-4" : "p-8",
          isDragging
            ? "border-primary bg-primary/5 shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.1)]"
            : "border-primary/25 bg-primary/[0.01]"
        )}
      >
        {queuedCount > 0 ? (
          <div className="flex items-center justify-center gap-2">
            <Upload className="h-4 w-4 text-primary/40" />
            <p className="text-xs text-muted-foreground">
              Drop more files here &middot; progress for every file is in
              &ldquo;Processing&rdquo; below
            </p>
          </div>
        ) : (
          <>
            <div
              className={cn(
                "mx-auto h-12 w-12 rounded-full flex items-center justify-center mb-3 transition-colors duration-200",
                isDragging ? "bg-primary/10" : "bg-primary/5"
              )}
            >
              <Upload
                className={cn(
                  "h-5 w-5 transition-colors duration-200",
                  isDragging ? "text-primary" : "text-primary/50"
                )}
              />
            </div>
            <p className="text-sm font-medium text-foreground">
              {"ontouchstart" in window ? "Tap to take a photo or select a file" : "Drag and drop files here"}
            </p>
            <p className="text-xs text-muted-foreground/60 mt-1.5">
              {mode === "statement" ? "Bank statements and order exports (CSV, XLSX, XLS, or PDF)" : mode === "receipt" ? "Receipts and invoices (PDF, JPG, PNG)" : "Statements and receipts together — bank statement PDFs are recognised and imported as statements"}
              {" "}&middot; Up to {MAX_FILES} files at a time
            </p>
            <p className="text-[11px] text-muted-foreground/60 mt-1">
              Stored on the machine running this server — not with a cloud provider.
            </p>
          </>
        )}
      </div>

      {/* Independent Browse button — completely separate from the drop zone */}
      <div className="flex items-center gap-3">
        <div className="flex-1 h-px bg-border/60" />
        <span className="text-xs text-muted-foreground/60 uppercase tracking-wider">or</span>
        <div className="flex-1 h-px bg-border/60" />
      </div>
      <Button
        type="button"
        variant="outline"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          const input = fileInputRef.current;
          if (!input) {
            console.warn("[UploadZone] fileInputRef is null — cannot open file picker");
            return;
          }
          // Reset value so re-selecting the same file still fires onChange.
          input.value = "";
          input.click();
        }}
        className={cn(
          "w-full h-11 gap-2 border-primary/30 transition-all",
          "hover:border-primary/50 hover:bg-primary/5",
          "active:scale-[0.98] active:bg-primary/10 active:border-primary/60"
        )}
      >
        <Upload className="h-4 w-4" />
        {mode === "statement" ? "Browse to upload statements" : mode === "receipt" ? "Browse to upload receipts" : "Browse files to upload"}
      </Button>

      {/* Rejected file type warning */}
      {rejectedMsg && (
        <div className="flex items-center gap-2 rounded-lg border border-destructive/20 bg-destructive/[0.03] px-3 py-2 text-sm text-destructive">
          <AlertCircle className="h-4 w-4 shrink-0" />
          <span>{rejectedMsg}</span>
        </div>
      )}

      {/* Previously uploaded receipts (persisted from backend) */}
      {mode === "receipt" && prevDocs.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Previously Uploaded ({prevDocs.length})
          </p>
          <div className="max-h-48 overflow-y-auto space-y-1.5">
            {prevDocs.map((doc) => (
              <div
                key={doc.id}
                className="flex items-center gap-3 rounded-lg border border-border/40 bg-muted/20 p-2.5"
              >
                <div className="flex-shrink-0 h-7 w-7 rounded-md bg-primary/5 flex items-center justify-center">
                  <FileText className="h-3.5 w-3.5 text-primary/50" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium truncate">{doc.filename}</p>
                  <p className="text-[10px] text-muted-foreground">
                    {doc.vendor ?? "Unknown"}{doc.date ? ` · ${doc.date}` : ""}
                    {doc.total != null ? ` · $${Number(doc.total).toFixed(2)}` : ""}
                  </p>
                </div>
                {doc.status === "MATCHED" && doc.matched_transaction_id ? (
                  <Badge
                    className="text-[10px] bg-primary/10 text-primary border-primary/20 cursor-pointer hover:bg-primary/20 transition-colors"
                    title="Click to view matched transaction"
                    onClick={(e) => {
                      e.stopPropagation();
                      const params = new URLSearchParams();
                      if (doc.matched_source_file) {
                        params.set("statement", doc.matched_source_file);
                      }
                      params.set("highlight", String(doc.matched_transaction_id));
                      onClose();
                      navigate(`/transactions?${params.toString()}`);
                    }}
                  >
                    Matched ↗
                  </Badge>
                ) : (
                  <Badge className="text-[10px] bg-primary/5 text-primary/60 border-primary/10">
                    {doc.status === "MATCHED" ? "Matched" : doc.status === "EXTRACTED" ? "Ready" : doc.status}
                  </Badge>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Previously uploaded statements (persisted from backend) */}
      {mode === "statement" && prevStatements.length > 0 && (
        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
            Previously Uploaded ({prevStatements.length})
          </p>
          <div className="max-h-48 overflow-y-auto space-y-1.5">
            {prevStatements.map((sf) => (
              <div
                key={sf.source_file}
                className="flex items-center gap-3 rounded-lg border border-border/40 bg-muted/20 p-2.5"
              >
                <div className="flex-shrink-0 h-7 w-7 rounded-md bg-primary/5 flex items-center justify-center">
                  <FileText className="h-3.5 w-3.5 text-primary/50" />
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-xs font-medium truncate">{sf.source_file}</p>
                  <p className="text-[10px] text-muted-foreground">
                    {sf.account} · {sf.transaction_count} transactions
                  </p>
                </div>
                <Badge className="text-[10px] bg-primary/5 text-primary/60 border-primary/10">
                  Imported
                </Badge>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
