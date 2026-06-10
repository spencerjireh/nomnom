// Prod relay. Hardcoded so onboarding only asks for the passphrase; overridable
// under "advanced" for a second relay or local dev. Matches relay.spencerjireh.com
// (see relay-worker/wrangler.toml) and the CORS allowlist in the Worker.
export const DEFAULT_RELAY_URL = "https://relay.spencerjireh.com";

// 30s long-poll, matching the Worker's MAX_BUDGET_MS and the CLI's
// _RELAY_DEFAULT_WAIT_MS. The Worker caps GET hold time at 30s regardless.
// Used by the roster long-poll and as the slot-discovery fallback when the SSE
// /stream endpoint is unavailable.
export const RELAY_WAIT_MS = 30_000;

// Backoff before reopening an SSE stream after the server's ~4-min cap or a
// transient drop. Each reopen re-signs the URL (fresh auth, current cursor).
export const STREAM_RECONNECT_MS = 1_000;

// If the stream errors this many times in a row WITHOUT ever opening, treat the
// relay as not supporting /stream (e.g. not yet deployed) and fall back to the
// long-poll loop for the rest of the session.
export const STREAM_UNSUPPORTED_RETRIES = 3;

// 100 MB practical cap (free-tier edge limit; Worker accepts up to 256 MiB).
export const MAX_PAYLOAD_BYTES = 100 * 1024 * 1024;
