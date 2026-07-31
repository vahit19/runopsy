import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built assets are served by runopsy-server from its own package directory, so the
// output lands there rather than in a dist/ nobody wires up. Relative asset paths keep
// it working however the server chooses to mount it.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "../runopsy-server/src/runopsy_server/static",
    emptyOutDir: true,
  },
  server: {
    // Development only: the API is loopback-bound and unauthenticated, so the dev
    // server proxies rather than the page reaching across an origin.
    proxy: { "/v1": "http://127.0.0.1:8765" },
  },
});
