import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// jsdom cannot execute type="module" scripts, so the smoke test needs a classic
// (IIFE) bundle. Same source, different output format -- this config exists only
// for `npm run smoke`.
export default defineConfig({
  plugins: [react()],
  define: { "process.env.NODE_ENV": '"production"' },
  build: {
    outDir: "dist-smoke",
    sourcemap: false,
    minify: false,
    cssCodeSplit: false,
    lib: { entry: "src/main.jsx", name: "ReplaySmoke", formats: ["iife"], fileName: () => "bundle.js" },
  },
});
