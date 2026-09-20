import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";
import fs from "fs";

// Read backend PORT from project root .env (single source of truth)
function getBackendPort(): number {
  try {
    const envPath = path.resolve(__dirname, "../.env");
    const envContent = fs.readFileSync(envPath, "utf-8");
    const match = envContent.match(/^PORT=(\d+)/m);
    if (match) return parseInt(match[1], 10);
  } catch {
    // .env not found — fall back to the same default config/settings.py uses.
  }
  return 8080;
}

const backendPort = getBackendPort();

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5174,
    strictPort: true,
    proxy: {
      "/api": {
        target:
          process.env.VITE_API_PROXY_TARGET ||
          `http://localhost:${backendPort}`,
        changeOrigin: true,
      },
    },
  },
});
