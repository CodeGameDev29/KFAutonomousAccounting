import { useEffect } from "react";
import { useLocation, useOutlet } from "react-router-dom";
import { motion, AnimatePresence, useReducedMotion } from "motion/react";
import { SidebarProvider, SidebarInset, SidebarTrigger } from "@/components/ui/sidebar";
import { AppSidebar } from "./AppSidebar";
import { BackForwardButtons } from "./BackForwardButtons";
import { Separator } from "@/components/ui/separator";
import { ProcessingHeaderIndicator } from "@/components/processing/ProcessingHeaderIndicator";
import { startProcessingQueueSync } from "@/stores/processing-queue-store";

const PAGE_TITLES: Record<string, string> = {
  "/dashboard": "Dashboard",
  "/transactions": "Transactions",
  "/review": "Review",
  "/reports": "Reports",
  "/analytics": "Analytics",
  "/activity": "Activity",
  "/settings": "Settings",
  "/onboarding": "Setup",
  "/security": "Security",
};

function getPageTitle(pathname: string): string {
  // Exact match first
  if (PAGE_TITLES[pathname]) return PAGE_TITLES[pathname];
  // Prefix match for nested routes (e.g., /reports/2026/1)
  for (const [path, title] of Object.entries(PAGE_TITLES)) {
    if (pathname.startsWith(path + "/")) return title;
  }
  return "";
}

export function AppLayout() {
  const location = useLocation();
  const prefersReducedMotion = useReducedMotion();
  const pageTitle = getPageTitle(location.pathname);
  // `useOutlet()` rather than `<Outlet />`, and this is load-bearing.
  //
  // `<Outlet />` is a component that reads the *current* route out of context
  // every time it renders. Inside `<AnimatePresence mode="wait">` the outgoing
  // `motion.div` keeps rendering for the length of its exit animation, so its
  // `<Outlet />` would resolve to the destination route: that page mounts
  // inside the dying wrapper, runs all of its effects and queries, is thrown
  // away when the exit finishes, and mounts a second time in the incoming
  // wrapper with fresh state.
  //
  // A double mount also loses anything a page consumes once — the pending
  // upload request, for instance, is taken by the short-lived instance and the
  // one left on screen has nothing to act on. Capturing the element here pins
  // each wrapper to the page it was rendered for, so every page mounts exactly
  // once per navigation.
  const outlet = useOutlet();

  // Load the processing queue once per authenticated session and keep it
  // current from here on, so the header chip and the panel are both right the
  // instant a page renders — including a hard reload in the middle of an
  // import that started before the browser was refreshed.
  useEffect(() => startProcessingQueueSync(), []);

  return (
    <SidebarProvider>
      <AppSidebar />
      {/* app-main: min-width:0 + overflow-x:clip so nothing inside can widen
          the document. See web/src/index.css. */}
      <SidebarInset className="app-main">
        <header className="flex h-16 shrink-0 items-center gap-2 border-b px-4 transition-[width,height] ease-linear group-has-[[data-collapsible=icon]]/sidebar-wrapper:h-12">
          <div className="flex min-w-0 items-center gap-2">
            <SidebarTrigger className="-ml-1" />
            <BackForwardButtons />
            <Separator orientation="vertical" className="mr-2 h-4" />
            {pageTitle && (
              <h1 className="text-sm font-semibold text-foreground truncate">
                {pageTitle}
              </h1>
            )}
            <Separator orientation="vertical" className="mx-1 h-4" />
            {/* Processing chip. In the header's normal flow, left-aligned, so
                it can never sit on top of a control the user is trying to
                click. It renders nothing at all when nothing is in flight. */}
            <ProcessingHeaderIndicator className="ml-1" />
          </div>
        </header>
        {/* pb-24 keeps the bottom strip of the viewport free of interactive
            controls, so the toast stack (bottom-right, 5rem inset) cannot land
            on one. */}
        <div className="flex min-w-0 flex-1 flex-col gap-4 p-4 pt-0 pb-24">
          {prefersReducedMotion ? (
            outlet
          ) : (
            <AnimatePresence mode="wait">
              <motion.div
                key={location.pathname}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
                transition={{ duration: 0.15, ease: "easeOut" }}
              >
                {outlet}
              </motion.div>
            </AnimatePresence>
          )}
        </div>
      </SidebarInset>
    </SidebarProvider>
  );
}
