import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      main: "src/worker.ts",
      miniflare: {
        compatibilityDate: "2025-05-01",
        r2Buckets: ["BUCKET"],
        durableObjects: {
          FEED_NOTIFIER: "FeedNotifier",
        },
        bindings: {
          NOMNOM_HMAC_SECRET: "test-secret-do-not-use-in-prod",
        },
      },
    }),
  ],
});
