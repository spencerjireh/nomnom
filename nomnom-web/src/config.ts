// Prod relay. Hardcoded so onboarding only asks for the passphrase; overridable
// under "advanced" for a second relay or local dev. Matches relay.spencerjireh.com
// (see relay-worker/wrangler.toml) and the CORS allowlist in the Worker.
export const DEFAULT_RELAY_URL = "https://relay.spencerjireh.com";

// WebSocket reconnect backoff: starts at MIN after a drop, doubles per
// consecutive failure up to MAX, resets once a socket opens. Each reconnect
// re-signs the URL (fresh auth, current seq cursor).
export const WS_RECONNECT_MIN_MS = 1_000;
export const WS_RECONNECT_MAX_MS = 30_000;

// Client-side keepalive. The relay answers "ping" with "pong" at the edge
// without waking the feed's Durable Object; this only keeps idle proxies from
// dropping the socket.
export const WS_PING_INTERVAL_MS = 30_000;

// 100 MB practical cap (free-tier edge limit; Worker accepts up to 256 MiB).
export const MAX_PAYLOAD_BYTES = 100 * 1024 * 1024;

// nomnom has exactly one "channel": a single feed shared across a user's own
// devices, stored under this fixed local name. The relay keeps a channel alive
// while it is used and purges it after 30 days of inactivity; posts age out 30
// days after creation. There is no client-side TTL.
export const CHANNEL_NAME = "channel";
