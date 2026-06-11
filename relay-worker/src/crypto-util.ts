// Shared crypto + auth helpers used by both auth schemes (relay HMAC + per-feed
// signature). Kept together because the MAC primitive, URL-safe base64, and the
// `Authorization: <prefix> <ts>:<mac>` parse path are crypto-adjacent and all
// three modules (auth.ts, feed-auth.ts, feeds.ts, tests/helpers.ts) need them.

export const TIMESTAMP_WINDOW_SEC = 300;

export type AuthResult =
  | { ok: true }
  | { ok: false; status: number; reason: string };

export function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

export function constantTimeEqualHex(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

export function urlsafeBase64Encode(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function urlsafeBase64Decode(s: string): Uint8Array {
  let padded = s.replace(/-/g, "+").replace(/_/g, "/");
  while (padded.length % 4 !== 0) padded += "=";
  const bin = atob(padded);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

/**
 * HMAC-SHA256 over msg, returning hex. `key` is bytes for derived keys (feed
 * key) and a string for the relay's raw deployment secret — string is encoded
 * to UTF-8 before importKey.
 */
export async function hmacSha256Hex(
  key: Uint8Array | string,
  msg: string,
): Promise<string> {
  const enc = new TextEncoder();
  const keyBytes = typeof key === "string" ? enc.encode(key) : key;
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", cryptoKey, enc.encode(msg));
  return bytesToHex(new Uint8Array(sig));
}

/**
 * Parse an `Authorization: <prefix> <ts>:<mac>` header and validate the timestamp
 * window. Both auth schemes use the same envelope; only the prefix and the error
 * reason strings differ.
 *
 * Returns `{ tsStr, macHex }` on success, or an `AuthResult` failure on any of:
 *   - header missing or wrong prefix       → 401 `missing-<...>`
 *   - malformed `<ts>:<mac>`                → 401 `bad-<...>`
 *   - timestamp drifts > ±TIMESTAMP_WINDOW  → 401 `clock-skew`
 */
export function parseSignedAuth(
  header: string,
  prefix: string,
  badReason: { missing: string; bad: string },
): { tsStr: string; macHex: string } | Extract<AuthResult, { ok: false }> {
  if (!header.startsWith(prefix)) {
    return { ok: false, status: 401, reason: badReason.missing };
  }
  const rest = header.slice(prefix.length);
  const colon = rest.indexOf(":");
  if (colon < 0) {
    return { ok: false, status: 401, reason: badReason.bad };
  }
  const tsStr = rest.slice(0, colon);
  const macHex = rest.slice(colon + 1).toLowerCase();
  const ts = Number.parseInt(tsStr, 10);
  if (!Number.isFinite(ts) || tsStr !== String(ts)) {
    return { ok: false, status: 401, reason: badReason.bad };
  }
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > TIMESTAMP_WINDOW_SEC) {
    return { ok: false, status: 401, reason: "clock-skew" };
  }
  return { tsStr, macHex };
}
