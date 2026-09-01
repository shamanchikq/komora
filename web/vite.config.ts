import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // In development the API runs on :8000; the OAuth callback and initData auth
    // work unchanged, the dev server just proxies so fetches stay same-origin.
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
