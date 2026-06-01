import { defineConfig } from "vitest/config";

// Separate from vite.config.ts on purpose: the crypto vectors are pure TS (no
// JSX), so the test runner needs no React plugin — and keeping the plugin out
// avoids a duplicate-Vite type clash between vite and vitest's bundled copy.
export default defineConfig({
  test: {
    globals: true,
    environment: "node",
    include: ["test/**/*.test.ts"],
  },
});
