import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite config for the RID dashboard.
// We expose the server on 0.0.0.0:5173 so it's reachable from another
// host during E2E tests, and proxy /api + /ws to the FastAPI server
// in docker (port 8000) — this avoids CORS plumbing and matches the
// "single origin" expectation of production deployments.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/ws": {
        target: "ws://localhost:8000",
        ws: true,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: true,
  },
});
