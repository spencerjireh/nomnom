// Relay request authentication header. Mirrors nomnom.py `_relay_hmac_headers`
// and is verified by relay-worker/src/auth.ts.
//
//   Authorization: NMNM-HMAC-SHA256 <unix_ts>:<hex_mac>
//   mac = HMAC-SHA256(secret, method + "\n" + bare_path + "\n" + unix_ts)
//
// The MAC covers (method, path-without-query, ts) — NOT the body. Body integrity
// comes from the AEAD wrapper. The Worker allows +/-300s clock skew.

import { hmacSha256 } from "./primitives";
import { bytesToHexDigest } from "./hex";
import { RELAY_AUTH_PREFIX } from "./constants";

const enc = new TextEncoder();

/**
 * Build the Authorization header value for one request. `nowSeconds` is injectable
 * for deterministic fixtures; production callers omit it to use the wall clock.
 * `path` must be the bare pathname (no query string).
 */
export function relayAuthHeader(
  secret: string,
  method: string,
  path: string,
  nowSeconds?: number,
): string {
  const ts = String(nowSeconds ?? Math.floor(Date.now() / 1000));
  const barePath = path.split("?", 1)[0];
  const msg = enc.encode(`${method}\n${barePath}\n${ts}`);
  const mac = bytesToHexDigest(hmacSha256(enc.encode(secret), msg));
  return `${RELAY_AUTH_PREFIX}${ts}:${mac}`;
}
