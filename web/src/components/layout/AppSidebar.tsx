import { useLocation, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import {
  LayoutDashboard,
  ArrowLeftRight,
  BarChart3,
  TrendingUp,
  ClipboardCheck,
  Settings,
  History,
  LogOut,
  ChevronsUpDown,
  Upload,
} from "lucide-react";
import { Logo } from "@/components/Logo";
import { useAuth } from "@/hooks/use-auth";
import { useUploadIntentStore } from "@/stores/upload-intent-store";
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from "@/components/ui/sidebar";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface NavItem {
  title: string;
  url: string;
  icon: React.ComponentType<{ className?: string }>;
  badge?: number;
}

function getInitials(email: string): string {
  const name = email.split("@")[0];
  if (!name) return "?";
  const parts = name.split(/[._-]/);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  return name.slice(0, 2).toUpperCase();
}

export function AppSidebar() {
  const location = useLocation();
  const navigate = useNavigate();
  const { user, signOut } = useAuth();
  const requestUpload = useUploadIntentStore((s) => s.requestUpload);

  const email = user?.email ?? "";
  const avatarUrl = user?.user_metadata?.avatar_url as string | undefined;
  const displayName =
    (user?.user_metadata?.full_name as string | undefined) ?? email;

  // Fetch review queue counts for sidebar badges
  const { data: queueData } = useQuery<{
    matches: unknown[];
    queue_counts: Record<string, number>;
  }>({
    queryKey: ["review-match-counts"],
    queryFn: () =>
      api.get(
        "/api/reconciliation/matches?status=PENDING_REVIEW,NEEDS_REVIEW&page=1&per_page=1"
      ),
    staleTime: 60_000,
    refetchInterval: 120_000,
  });

  const reviewCount =
    (queueData?.queue_counts?.["PENDING_REVIEW"] ?? 0) +
    (queueData?.queue_counts?.["NEEDS_REVIEW"] ?? 0);

  const bookkeepingItems: NavItem[] = [
    {
      title: "Dashboard",
      url: "/dashboard",
      icon: LayoutDashboard,
    },
    {
      title: "Transactions",
      url: "/transactions",
      icon: ArrowLeftRight,
    },
    {
      title: "Review",
      url: "/review",
      icon: ClipboardCheck,
      badge: reviewCount > 0 ? reviewCount : undefined,
    },
    {
      title: "Reports",
      url: "/reports",
      icon: BarChart3,
    },
    {
      title: "Analytics",
      url: "/analytics",
      icon: TrendingUp,
    },
  ];

  const accountItems: NavItem[] = [
    {
      title: "Activity",
      url: "/activity",
      icon: History,
    },
    {
      title: "Settings",
      url: "/settings",
      icon: Settings,
    },
  ];

  const handleSignOut = async () => {
    await signOut();
    navigate("/");
  };

  return (
    <Sidebar collapsible="icon" className="border-r border-border">
      {/* Header with brand mark */}
      <SidebarHeader className="pb-0">
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton
              size="lg"
              className="data-[state=open]:bg-primary/10 data-[state=open]:text-primary hover:bg-primary/5"
              onClick={() => navigate("/dashboard")}
            >
              <Logo size="sm" />
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold text-foreground">
                  Autonomous Accounting
                </span>
              </div>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      {/* Subtle separator */}
      <div className="mx-3 my-2 border-t border-border/50" />

      <SidebarContent>
        <SidebarGroup>
          <SidebarGroupLabel className="text-muted-foreground uppercase text-[10px] tracking-widest font-semibold">
            Bookkeeping
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {bookkeepingItems.map((item) => {
                const isActive =
                  location.pathname === item.url ||
                  location.pathname.startsWith(item.url + "/");
                return (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton
                      isActive={isActive}
                      onClick={() => navigate(item.url)}
                      tooltip={item.title}
                      className={
                        isActive
                          ? "bg-primary/10 text-primary font-medium hover:bg-primary/10"
                          : "text-muted-foreground hover:bg-primary/5 hover:text-primary"
                      }
                    >
                      <item.icon className={`size-4 ${isActive ? "text-primary" : ""}`} />
                      <span>{item.title}</span>
                    </SidebarMenuButton>
                    {item.badge !== undefined && item.badge > 0 && (
                      <SidebarMenuBadge className="bg-amber-600 text-white border-0 text-[10px]">
                        {item.badge}
                      </SidebarMenuBadge>
                    )}
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {/* Quick upload action */}
        <SidebarGroup>
          <SidebarGroupContent>
            <SidebarMenu>
              <SidebarMenuItem>
                <SidebarMenuButton
                  onClick={() => {
                    // Record the request in the store, then navigate only when
                    // the page is not already open. Navigating to
                    // "/transactions" from "/transactions?statement=…&page=3"
                    // would throw away the view the user was looking at, and a
                    // "?upload=1" round-trip does nothing once the page is
                    // mounted.
                    requestUpload("auto");
                    if (location.pathname !== "/transactions") {
                      navigate("/transactions");
                    }
                  }}
                  tooltip="Upload Documents"
                  className="text-primary hover:bg-primary/5 hover:text-primary font-medium"
                >
                  <Upload className="size-4" />
                  <span>Upload Documents</span>
                </SidebarMenuButton>
              </SidebarMenuItem>
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        <SidebarGroup>
          <SidebarGroupLabel className="text-muted-foreground uppercase text-[10px] tracking-widest font-semibold">
            Account
          </SidebarGroupLabel>
          <SidebarGroupContent>
            <SidebarMenu>
              {accountItems.map((item) => {
                const isActive =
                  location.pathname === item.url ||
                  location.pathname.startsWith(item.url + "/");
                return (
                  <SidebarMenuItem key={item.title}>
                    <SidebarMenuButton
                      isActive={isActive}
                      onClick={() => navigate(item.url)}
                      tooltip={item.title}
                      className={
                        isActive
                          ? "bg-primary/10 text-primary font-medium hover:bg-primary/10"
                          : "text-muted-foreground hover:bg-primary/5 hover:text-primary"
                      }
                    >
                      <item.icon className={`size-4 ${isActive ? "text-primary" : ""}`} />
                      <span>{item.title}</span>
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                );
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>
      </SidebarContent>

      {/* Footer with user section */}
      <SidebarFooter>
        {/* Separator above footer */}
        <div className="mx-3 mb-2 border-t border-border/50" />
        <SidebarMenu>
          <SidebarMenuItem>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <SidebarMenuButton
                  size="lg"
                  className="data-[state=open]:bg-primary/10 data-[state=open]:text-primary hover:bg-primary/5"
                >
                  <Avatar className="h-8 w-8 rounded-lg">
                    {avatarUrl && (
                      <AvatarImage src={avatarUrl} alt={displayName} />
                    )}
                    <AvatarFallback className="rounded-lg bg-primary/10 text-primary text-xs font-semibold">
                      {getInitials(email)}
                    </AvatarFallback>
                  </Avatar>
                  <div className="grid flex-1 text-left text-sm leading-tight">
                    <span className="truncate font-semibold text-foreground">
                      {displayName}
                    </span>
                    <span className="truncate text-xs text-muted-foreground">
                      {email}
                    </span>
                  </div>
                  <ChevronsUpDown className="ml-auto size-4 text-muted-foreground" />
                </SidebarMenuButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent
                className="w-[--radix-dropdown-menu-trigger-width] min-w-56 rounded-lg"
                side="bottom"
                align="end"
                sideOffset={4}
              >
                <DropdownMenuLabel className="p-0 font-normal">
                  <div className="flex items-center gap-2 px-1 py-1.5 text-left text-sm">
                    <Avatar className="h-8 w-8 rounded-lg">
                      {avatarUrl && (
                        <AvatarImage src={avatarUrl} alt={displayName} />
                      )}
                      <AvatarFallback className="rounded-lg bg-primary/10 text-primary text-xs font-semibold">
                        {getInitials(email)}
                      </AvatarFallback>
                    </Avatar>
                    <div className="grid flex-1 text-left text-sm leading-tight">
                      <span className="truncate font-semibold">
                        {displayName}
                      </span>
                      <span className="truncate text-xs text-muted-foreground">
                        {email}
                      </span>
                    </div>
                  </div>
                </DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => navigate("/settings")}>
                  <Settings className="mr-2 h-4 w-4" />
                  Settings
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={handleSignOut}>
                  <LogOut className="mr-2 h-4 w-4" />
                  Sign out
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  );
}
