import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `reviewbot report runs/<id>` writes report.json; drop it (or symlink it) into
// public/ and the page renders the real run instead of the synthetic fixture.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  build: { outDir: "dist", sourcemap: true },
});
