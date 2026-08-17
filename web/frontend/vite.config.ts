import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev workflow: `npm run dev` here (Vite on :5173) + `python server.py`
// in the backend (FastAPI on :9000) in a second terminal. The proxy below
// forwards /ws (and any /api calls we add later) to the real backend, so
// the browser only ever talks to one origin during dev. In production,
// `npm run build` outputs to dist/ and server.py serves that directly —
// no proxy, no second process.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/ws": {
        target: "ws://127.0.0.1:8000",
        ws: true,
      },
    },
  },
  build: {
    outDir: "dist",
  },
});
