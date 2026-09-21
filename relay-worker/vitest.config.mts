import { cloudflareTest } from "@cloudflare/vitest-pool-workers";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [
    cloudflareTest({
      // Bindings, compatibility date, and the DO migrations (which is what
      // enables SQLite on the `Feed` class) come from wrangler.toml so the
      // tests cannot drift from the deployed config.
      wrangler: { configPath: "./wrangler.toml" },
      miniflare: {
        bindings: {
          NOMNOM_HMAC_SECRET: "test-secret-do-not-use-in-prod",
        },
      },
    }),
  ],
});
