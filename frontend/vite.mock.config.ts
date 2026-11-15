import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  // Static demo build served from https://<user>.github.io/beaconwatch/
  base: "/beaconwatch/",
  // This config is only used for the static demo.  Defining the flag
  // here means `npm run build:mock` cannot accidentally produce a
  // production build that calls a nonexistent backend (which is what
  // CI and local smoke tests used to do).
  define: {
    "import.meta.env.VITE_MOCK": JSON.stringify("1"),
  },
  plugins: [react()],
  build: {
    outDir: "dist",
    sourcemap: true,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (id.includes("maplibre-gl")) return "maplibre";
          return undefined;
        },
      },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5174,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8002",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/ws": { target: "ws://127.0.0.1:8002", ws: true, changeOrigin: true },
    },
  },
});
