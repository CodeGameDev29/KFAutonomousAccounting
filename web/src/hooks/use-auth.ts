import { useEffect } from "react";
import { useAuthStore } from "@/stores/auth-store";

/**
 * Hook that initializes the auth listener on mount and returns auth state.
 * Call this once at the app root (e.g., in App.tsx or main.tsx).
 */
export function useAuthInit() {
  const initialize = useAuthStore((s) => s.initialize);

  useEffect(() => {
    const unsubscribe = initialize();
    return unsubscribe;
  }, [initialize]);
}

/**
 * Convenience hook to access auth state in components.
 */
export function useAuth() {
  const user = useAuthStore((s) => s.user);
  const session = useAuthStore((s) => s.session);
  const loading = useAuthStore((s) => s.loading);
  const initialized = useAuthStore((s) => s.initialized);
  const onboarded = useAuthStore((s) => s.onboarded);
  const onboardingChecked = useAuthStore((s) => s.onboardingChecked);
  const emailVerified = useAuthStore((s) => s.emailVerified);
  const signInWithEmail = useAuthStore((s) => s.signInWithEmail);
  const signUp = useAuthStore((s) => s.signUp);
  const signOut = useAuthStore((s) => s.signOut);

  return {
    user,
    session,
    loading,
    initialized,
    onboarded,
    onboardingChecked,
    emailVerified,
    isAuthenticated: !!user,
    signInWithEmail,
    signUp,
    signOut,
  };
}
