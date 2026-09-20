import { BrowserRouter, Routes, Route } from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "@/lib/query-client";
import { useAuthInit } from "@/hooks/use-auth";
import { useSSE } from "@/hooks/use-sse";
import { useInstanceInfoSync } from "@/hooks/use-system-info";
import { AppLayout } from "@/components/layout/AppLayout";
import { PublicLayout } from "@/components/layout/PublicLayout";
import { ProtectedRoute } from "@/components/ProtectedRoute";
import { Landing } from "@/pages/Landing";
import { Login } from "@/pages/Login";
import { Dashboard } from "@/pages/Dashboard";
import { Transactions } from "@/pages/Transactions";
import { Reports } from "@/pages/Reports";
import { Analytics } from "@/pages/Analytics";
import { Settings } from "@/pages/Settings";
import { Onboarding } from "@/pages/Onboarding";
import { ReconciliationReview } from "@/pages/ReconciliationReview";
import { Security } from "@/pages/Security";
import { Activity } from "@/pages/Activity";
import { SharedReport } from "@/pages/SharedReport";
import OAuthCallback from "@/pages/OAuthCallback";
import { ResetConfirm } from "@/pages/ResetConfirm";
import { GoogleComplete } from "@/pages/GoogleComplete";
import { NotFound } from "@/pages/NotFound";
import { PrivacyPolicy } from "@/pages/PrivacyPolicy";
import { Signup } from "@/pages/Signup";
import { TermsOfService } from "@/pages/TermsOfService";
import { CommandPalette } from "@/components/CommandPalette";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { SampleReport } from "@/pages/SampleReport";
import { Toaster } from "sonner";

function AppInner() {
  // Initialize auth listener (subscribes to authClient onAuthStateChange)
  useAuthInit();

  // Connect SSE for real-time cache invalidation
  useSSE();

  // Read `/health` once at the root: pages that describe this instance (the
  // Security cards, the confidence legend, the upload-limit wording) then have
  // the server's answer on their first paint rather than a fallback.
  useInstanceInfoSync();

  return (
    <>
    <CommandPalette />
    <Routes>
      {/* Public routes */}
      <Route element={<PublicLayout />}>
        <Route path="/" element={<Landing />} />
        <Route path="/login" element={<Login />} />
        <Route path="/shared/:token" element={<SharedReport />} />
        <Route path="/privacy" element={<PrivacyPolicy />} />
        {/* `/signup` and `/sign-up` both render the canonical signup form.
            Query params are preserved. */}
        <Route path="/signup" element={<Signup />} />
        <Route path="/sign-up" element={<Signup />} />
        <Route path="/terms" element={<TermsOfService />} />
        <Route path="/sample-report" element={<SampleReport />} />
      </Route>

      {/* OAuth callback (no auth required — handles popup flow) */}
      <Route path="/auth/gmail/callback" element={<OAuthCallback />} />

      {/* Password reset confirm — intermediate page that defers the verify GET
          until a real user click, so email-safety scanners can't consume the
          one-time token before the user sees it. */}
      <Route path="/auth/reset-confirm" element={<ResetConfirm />} />

      {/* Protected: onboarding (no sidebar, but requires auth) */}
      <Route
        path="/onboarding"
        element={
          <ProtectedRoute requireOnboarding={false}>
            <Onboarding />
          </ProtectedRoute>
        }
      />

      {/* Where Google sends the browser back. Public: the user is not
          authenticated yet - trading the ticket is what signs them in. */}
      <Route path="/auth/google/complete" element={<GoogleComplete />} />

      {/* Protected: app shell with sidebar */}
      <Route
        element={
          <ProtectedRoute>
            <AppLayout />
          </ProtectedRoute>
        }
      >
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/transactions" element={<Transactions />} />
        <Route path="/reports" element={<Reports />} />
        <Route path="/reports/:year" element={<Reports />} />
        <Route path="/reports/:year/:month" element={<Reports />} />
        <Route path="/analytics" element={<Analytics />} />
        <Route path="/review" element={<ReconciliationReview />} />
        <Route path="/activity" element={<Activity />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/security" element={<Security />} />
      </Route>

      {/* 404 */}
      <Route path="*" element={<NotFound />} />
    </Routes>
    {/* The toast stack keeps a 5rem inset so a toast cannot land on a control
        at the bottom of a page — the app shell reserves pb-24 for it. */}
    <Toaster
      position="bottom-right"
      offset="5rem"
      toastOptions={{
        className: "shadow-warm-sm border-border/60",
        duration: 4000,
      }}
      richColors
    />
    </>
  );
}

export default function App() {
  return (
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <AppInner />
        </BrowserRouter>
      </QueryClientProvider>
    </ErrorBoundary>
  );
}
