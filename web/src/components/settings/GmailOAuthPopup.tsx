import { useCallback, useEffect, useRef } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";

interface GmailOAuthPopupProps {
  onSuccess: () => void;
  onError: (error: string) => void;
}

export function useGmailOAuth({ onSuccess, onError }: GmailOAuthPopupProps) {
  const queryClient = useQueryClient();
  const popupRef = useRef<Window | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const getAuthUrl = useMutation({
    mutationFn: () =>
      api.post<{ auth_url: string }>("/api/onboarding/gmail/auth-url"),
  });

  const handleMessage = useCallback(
    (event: MessageEvent) => {
      if (event.origin !== window.location.origin) return;
      if (event.data?.type !== "gmail_oauth_complete") return;

      if (intervalRef.current) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }

      if (event.data.success) {
        queryClient.invalidateQueries({ queryKey: ["onboarding"] });
        queryClient.invalidateQueries({ queryKey: ["dashboard", "checklist"] });
        onSuccess();
      } else {
        onError(event.data.error || "Unknown error");
      }
    },
    [onSuccess, onError, queryClient],
  );

  useEffect(() => {
    window.addEventListener("message", handleMessage);
    return () => {
      window.removeEventListener("message", handleMessage);
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [handleMessage]);

  const openPopup = useCallback(async () => {
    try {
      const data = await getAuthUrl.mutateAsync();
      const popup = window.open(
        data.auth_url,
        "gmail-oauth",
        "width=500,height=600,left=200,top=100",
      );

      if (!popup) {
        onError("Popup blocked — please allow popups for this site");
        return;
      }

      popupRef.current = popup;

      // Poll for popup closed without message
      intervalRef.current = setInterval(() => {
        if (popup.closed) {
          if (intervalRef.current) {
            clearInterval(intervalRef.current);
            intervalRef.current = null;
          }
        }
      }, 500);
    } catch {
      onError("Failed to start Gmail connection");
    }
  }, [getAuthUrl, onError]);

  return { openPopup, isLoading: getAuthUrl.isPending };
}
