import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies the API to `chatbot serve` (port 8000).
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/send": "http://127.0.0.1:8000",
      "/threads": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/agent": "http://127.0.0.1:8000",
    },
  },
});
