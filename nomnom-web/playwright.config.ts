import { defineConfig, devices } from "@playwright/test";

// Secret-free UI smoke test. Drives the *built* app through `vite preview` and
// never touches the real relay: the only network call onboarding makes (GET
// /health) is mocked per-test, and every other screen is reachable from
// localStorage alone.
//
// This deliberately does NOT cover CLI<->browser interop — that needs the live
// relay, the relay passphrase, and a running `nomnom` CLI, none of which belong
// in CI. Interop is proven two other ways: the cross-language vitest vectors
// (`npm test`) and a manual three-leg round-trip (pair / send / receive). This
// suite guards the UI + state wiring from regressing.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI
    ? [["list"], ["html", { open: "never" }]]
    : "html",
  use: {
    baseURL: "http://localhost:4173",
    trace: "on-first-retry",
  },
  // Self-building so a stale dist can never mask a regression, locally or in CI.
  webServer: {
    command: "npm run build && npm run preview -- --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
