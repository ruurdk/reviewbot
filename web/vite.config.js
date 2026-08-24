import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// A completed `reviewbot run <id>` writes runs/<id>/report.json -- the full
// report, with the per_pr rows and quality blocks this page reads. (`reviewbot
// report` writes summary.json, the accounting block alone, which is NOT enough.)
// Copy or symlink report.json into public/ and the page renders the real run
// instead of the synthetic fixture.
export default defineConfig({
  plugins: [react()],
  server: { port: 5173 },
  build: { outDir: "dist", sourcemap: true },
});
