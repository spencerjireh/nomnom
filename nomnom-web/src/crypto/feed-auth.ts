// Authorization header for /feeds/:id/* requests. Mirrors nomnom.py
// `_feed_auth_headers` and is verified by relay-worker/src/feed-auth.ts.
//
//   Authorization: NMNM-FEEDKEY-SHA256 <unix_ts>:<hex_mac>
//   mac = HMAC-SHA256(feed_key, method + "\n" + bare_path + "\n" + unix_ts)
//
// The query string is stripped so the signed path matches the Worker's
// url.pathname. The Worker allows +/-300s clock skew.

import { feedRequestMac } from "./feeds";
import { FEED_AUTH_PREFIX } from "./constants";

/**
 * Build the feed-key Authorization header for one request. `path` must be the
 * full pathname the Worker will see; any query string is stripped before
 * signing. `nowSeconds` is injectable for deterministic fixtures.
 */
export function feedAuthHeader(
  feedKey: Uint8Array,
  method: string,
  path: string,
  nowSeconds?: number,
): string {
  return `${FEED_AUTH_PREFIX}${feedAuthEnvelope(feedKey, method, path, nowSeconds)}`;
}

/**
 * The bare `<ts>:<mac>` envelope, without the scheme prefix. The WebSocket
 * endpoint takes it as `?auth=` because a browser socket can't set headers.
 */
export function feedAuthEnvelope(
  feedKey: Uint8Array,
  method: string,
  path: string,
  nowSeconds?: number,
): string {
  const ts = nowSeconds ?? Math.floor(Date.now() / 1000);
  const barePath = path.split("?", 1)[0];
  return `${ts}:${feedRequestMac(feedKey, method, barePath, ts)}`;
}
