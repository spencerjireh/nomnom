// Feed-key signature verification for /feeds/:id/* endpoints.
//
// Authorization: NMNM-FEEDKEY-SHA256 <unix_ts>:<hex_mac>
//   feed_key = HKDF-SHA256(
//                salt="nomnom-feed-v1",
//                ikm=urlsafeBase64Decode(feed_id),
//                info=feed_id,
//                length=32,
//              )
//   mac      = HMAC-SHA256(feed_key, method + "\n" + path + "\n" + unix_ts)
//
// Same shape as the deployment-wide HMAC scheme, keyed by a per-feed key derived
// from the URL token. Possession of the feed URL grants access to that feed only;
// leaks don't compromise the rest of the relay.

import {
  bytesToHex,
  constantTimeEqualHex,
  urlsafeBase64Decode,
} from "./crypto-util";

const TIMESTAMP_WINDOW_SEC = 300;
const AUTH_PREFIX = "NMNM-FEEDKEY-SHA256 ";
const HKDF_SALT = new TextEncoder().encode("nomnom-feed-v1");

export type AuthResult =
  | { ok: true }
  | { ok: false; status: number; reason: string };

export async function deriveFeedKey(feedId: string): Promise<Uint8Array> {
  const ikm = urlsafeBase64Decode(feedId);
  const info = new TextEncoder().encode(feedId);
  const baseKey = await crypto.subtle.importKey(
    "raw",
    ikm,
    "HKDF",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: HKDF_SALT, info },
    baseKey,
    256,
  );
  return new Uint8Array(bits);
}

export async function verifyFeedKey(
  req: Request,
  feedId: string,
): Promise<AuthResult> {
  const header = req.headers.get("Authorization") ?? "";
  if (!header.startsWith(AUTH_PREFIX)) {
    return { ok: false, status: 401, reason: "missing-feed-mac" };
  }
  const rest = header.slice(AUTH_PREFIX.length);
  const colon = rest.indexOf(":");
  if (colon < 0) {
    return { ok: false, status: 401, reason: "bad-feed-mac" };
  }
  const tsStr = rest.slice(0, colon);
  const macHex = rest.slice(colon + 1).toLowerCase();
  const ts = Number.parseInt(tsStr, 10);
  if (!Number.isFinite(ts) || tsStr !== String(ts)) {
    return { ok: false, status: 401, reason: "bad-feed-mac" };
  }
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - ts) > TIMESTAMP_WINDOW_SEC) {
    return { ok: false, status: 401, reason: "clock-skew" };
  }

  let feedKey: Uint8Array;
  try {
    feedKey = await deriveFeedKey(feedId);
  } catch {
    return { ok: false, status: 403, reason: "bad-feed-id" };
  }

  const url = new URL(req.url);
  const msg = `${req.method}\n${url.pathname}\n${tsStr}`;
  const expected = await hmacSha256Hex(feedKey, msg);
  if (!constantTimeEqualHex(expected, macHex)) {
    return { ok: false, status: 401, reason: "bad-feed-mac" };
  }
  return { ok: true };
}

async function hmacSha256Hex(key: Uint8Array, msg: string): Promise<string> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    cryptoKey,
    new TextEncoder().encode(msg),
  );
  return bytesToHex(new Uint8Array(sig));
}
