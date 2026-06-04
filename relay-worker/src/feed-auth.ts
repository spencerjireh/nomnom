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
  constantTimeEqualHex,
  hmacSha256Hex,
  parseSignedAuth,
  urlsafeBase64Decode,
  type AuthResult,
} from "./crypto-util";

export type { AuthResult } from "./crypto-util";

const AUTH_PREFIX = "NMNM-FEEDKEY-SHA256 ";
const HKDF_SALT = new TextEncoder().encode("nomnom-feed-v1");

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
  const parsed = parseSignedAuth(header, AUTH_PREFIX, {
    missing: "missing-feed-mac",
    bad: "bad-feed-mac",
  });
  if ("ok" in parsed) return parsed;

  let feedKey: Uint8Array;
  try {
    feedKey = await deriveFeedKey(feedId);
  } catch {
    return { ok: false, status: 403, reason: "bad-feed-id" };
  }

  const url = new URL(req.url);
  const msg = `${req.method}\n${url.pathname}\n${parsed.tsStr}`;
  const expected = await hmacSha256Hex(feedKey, msg);
  if (!constantTimeEqualHex(expected, parsed.macHex)) {
    return { ok: false, status: 401, reason: "bad-feed-mac" };
  }
  return { ok: true };
}
