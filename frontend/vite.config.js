import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// `npm run dev` serves the app on :5173 and forwards /api (including the event stream) to the
// FastAPI server on :8000. `npm run build` writes dist/, which FastAPI serves in production.
export default defineConfig({
  plugins: [vue()],
  server: {
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
  build: { outDir: "dist", emptyOutDir: true, chunkSizeWarningLimit: 700 },
});
