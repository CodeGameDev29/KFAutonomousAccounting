import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

export default function OAuthCallback() {
  const [params] = useSearchParams();

  useEffect(() => {
    const code = params.get("code");
    const state = params.get("state");

    if (code && state) {
      // Redirect to the backend GET callback
      const backendUrl = `${import.meta.env.VITE_API_URL || ""}/api/onboarding/gmail/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
      window.location.href = backendUrl;
    }
  }, [params]);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <p className="text-muted-foreground">Connecting Gmail...</p>
    </div>
  );
}
