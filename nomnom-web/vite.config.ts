import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The crypto Web Worker is bundled as an ES module (worker.format: "es") so it
// can `import` the shared crypto/ module. build.target matches the relay-worker
// TS config (ES2022) — BigInt literals and top-level features need it.
export default defineConfig({
  plugins: [react()],
  worker: { format: "es" },
  build: { target: "es2022" },
  server: { port: 5173 },
});
