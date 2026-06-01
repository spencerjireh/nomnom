// Prod relay. Hardcoded so onboarding only asks for the passphrase; overridable
// under "advanced" for a second relay or local dev. Matches relay.spencerjireh.com
// (see relay-worker/wrangler.toml) and the CORS allowlist in the Worker.
export const DEFAULT_RELAY_URL = "https://relay.spencerjireh.com";

// 30s long-poll, matching the Worker's MAX_BUDGET_MS and the CLI's
// _RELAY_DEFAULT_WAIT_MS. The Worker caps GET hold time at 30s regardless.
export const RELAY_WAIT_MS = 30_000;

// 100 MB practical cap (free-tier edge limit; Worker accepts up to 256 MiB).
export const MAX_PAYLOAD_BYTES = 100 * 1024 * 1024;
