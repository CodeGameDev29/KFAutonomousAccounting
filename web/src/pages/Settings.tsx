import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api } from "@/lib/api";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuth } from "@/hooks/use-auth";
import { ErrorDisplay } from "@/components/shared/ErrorDisplay";
import { ProfileForm } from "@/components/settings/ProfileForm";
import { ConnectionsTab } from "@/components/settings/ConnectionsTab";
import { SecurityTab } from "@/pages/Security";

interface OnboardingConfig {
  profile: {
    company_name?: string;
    fiscal_year?: number;
    base_currency?: string;
    tax_jurisdiction?: string;
    business_number?: string;
    province?: string;
    industry?: string;
    naics_code?: string;
  };
  sources?: {
    gmail?: { enabled: boolean; connected: boolean; vendor_watchlist?: unknown[] };
    wise?: { enabled: boolean; connected: boolean };
  };
}

const TABS = ["profile", "connections", "security"] as const;

export function Settings() {
  const { user } = useAuth();
  const [searchParams, setSearchParams] = useSearchParams();

  const requestedTab = searchParams.get("tab") ?? "profile";
  const defaultTab = (TABS as readonly string[]).includes(requestedTab)
    ? requestedTab
    : "profile";

  const { data: config, isLoading, isError, error, refetch } = useQuery<OnboardingConfig>({
    queryKey: ["onboarding-config"],
    queryFn: () => api.get("/api/onboarding/config"),
    placeholderData: { profile: {} },
  });

  function handleTabChange(value: string) {
    if (value === "profile") {
      searchParams.delete("tab");
    } else {
      searchParams.set("tab", value);
    }
    setSearchParams(searchParams, { replace: true });
  }

  if (isError) {
    return (
      <ErrorDisplay error={error} context="settings" onRetry={refetch} />
    );
  }

  return (
    <div className="space-y-6 pt-4">
      <div>
        <h2 className="text-3xl font-bold tracking-tight">Settings</h2>
        <p className="text-muted-foreground">
          Manage your account and connections.
          {user?.email && (
            <span className="ml-1">
              Signed in as <span className="font-medium">{user.email}</span>.
            </span>
          )}
        </p>
      </div>

      <Tabs value={defaultTab} onValueChange={handleTabChange} className="space-y-6">
        <TabsList>
          <TabsTrigger value="profile">Profile</TabsTrigger>
          <TabsTrigger value="connections">Connections</TabsTrigger>
          <TabsTrigger value="security">Security</TabsTrigger>
        </TabsList>

        <TabsContent value="profile">
          <ProfileForm config={config?.profile ?? null} isLoading={isLoading} />
        </TabsContent>

        <TabsContent value="connections">
          <ConnectionsTab config={config ?? null} isLoading={isLoading} />
        </TabsContent>

        <TabsContent value="security">
          <SecurityTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
