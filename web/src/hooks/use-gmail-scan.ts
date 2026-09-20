import { useCallback, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

export interface ScanRun {
  id: number;
  source: string;
  status: "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  since_date: string | null;
  until_date: string | null;
  emails_scanned: number;
  documents_found: number;
  documents_added: number;
  documents_skipped: number;
  error_message: string | null;
  started_at: string | null;
  completed_at: string | null;
}

interface ScanStatusResponse {
  scan: ScanRun | null;
  is_active: boolean;
}

interface ScanHistoryResponse {
  runs: ScanRun[];
}

interface ScanProgress {
  run_id: number;
  phase: string;
  detail: string;
}

export function useGmailScan() {
  const queryClient = useQueryClient();
  const [scanProgress, setScanProgress] = useState<ScanProgress | null>(null);

  // Poll scan status
  const {
    data: statusData,
    isLoading: statusLoading,
  } = useQuery<ScanStatusResponse>({
    queryKey: ["scan-status"],
    queryFn: () => api.get("/api/gather/scan/status"),
    refetchInterval: (query) => {
      // Poll every 3s while a scan is active
      return query.state.data?.is_active ? 3000 : false;
    },
  });

  // Scan history
  const {
    data: historyData,
    isLoading: historyLoading,
  } = useQuery<ScanHistoryResponse>({
    queryKey: ["scan-history"],
    queryFn: () => api.get("/api/gather/history"),
  });

  // Start scan mutation
  const startScan = useMutation({
    mutationFn: (params: { since_date?: string; until_date?: string }) =>
      api.post<{ status: string; run_id: number }>("/api/gather/scan", params),
    onSuccess: () => {
      setScanProgress(null);
      queryClient.invalidateQueries({ queryKey: ["scan-status"] });
    },
  });

  // Listen for SSE scan events
  const handleScanProgress = useCallback((e: Event) => {
    const detail = (e as CustomEvent).detail as ScanProgress;
    setScanProgress(detail);
  }, []);

  const handleScanComplete = useCallback(() => {
    setScanProgress(null);
    queryClient.invalidateQueries({ queryKey: ["scan-status"] });
    queryClient.invalidateQueries({ queryKey: ["scan-history"] });
    queryClient.invalidateQueries({ queryKey: ["receipts"] });
    queryClient.invalidateQueries({ queryKey: ["connections", "health"] });
  }, [queryClient]);

  const handleScanFailed = useCallback(() => {
    setScanProgress(null);
    queryClient.invalidateQueries({ queryKey: ["scan-status"] });
    queryClient.invalidateQueries({ queryKey: ["scan-history"] });
  }, [queryClient]);

  useEffect(() => {
    window.addEventListener("scan_progress", handleScanProgress);
    window.addEventListener("scan_complete", handleScanComplete);
    window.addEventListener("scan_failed", handleScanFailed);
    return () => {
      window.removeEventListener("scan_progress", handleScanProgress);
      window.removeEventListener("scan_complete", handleScanComplete);
      window.removeEventListener("scan_failed", handleScanFailed);
    };
  }, [handleScanProgress, handleScanComplete, handleScanFailed]);

  return {
    // Current scan state
    activeScan: statusData?.scan,
    isScanning: statusData?.is_active ?? false,
    scanProgress,
    statusLoading,

    // Actions
    startScan,

    // History
    scanHistory: historyData?.runs ?? [],
    historyLoading,
  };
}
