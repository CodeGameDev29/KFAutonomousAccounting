import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// This app registers no service worker. A browser that picked one up from
// something else served on the same origin would keep serving its cached
// HTML/JS instead of this build, so unregister every worker and drop all
// CacheStorage entries before anything else runs. Best-effort and async —
// it must never delay or break startup, and it is a no-op when nothing is
// registered.
if ("serviceWorker" in navigator) {
  navigator.serviceWorker
    .getRegistrations()
    .then((regs) => Promise.all(regs.map((reg) => reg.unregister())))
    .catch(() => {});
}
if (typeof caches !== "undefined" && caches?.keys) {
  caches
    .keys()
    .then((keys) => Promise.all(keys.map((key) => caches.delete(key))))
    .catch(() => {});
}

// Nothing in the browser is instrumented: no analytics, no error reporter, no
// session recorder. The server's own logs are the only record of anything.

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
