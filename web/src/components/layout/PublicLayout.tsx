import { useEffect } from "react";
import { Outlet, useLocation } from "react-router-dom";

// Set <link rel="canonical"> to the current route on every navigation.
// Without this, the static index.html canonical (root) is served for every
// SPA route, and every public route reports as a duplicate of the root.
//
// The origin is read from the browser rather than baked in: a self-hosted
// instance is reached at whatever hostname its operator gave it, and this
// software has no idea what that is.
function useCanonical() {
  const { pathname } = useLocation();
  useEffect(() => {
    const href = `${window.location.origin}${pathname === "/" ? "/" : pathname.replace(/\/+$/, "")}`;
    let link = document.querySelector<HTMLLinkElement>('link[rel="canonical"]');
    if (!link) {
      link = document.createElement("link");
      link.rel = "canonical";
      document.head.appendChild(link);
    }
    link.href = href;
  }, [pathname]);
}

export function PublicLayout() {
  useCanonical();
  return (
    <div className="min-h-screen bg-background">
      <Outlet />
    </div>
  );
}
