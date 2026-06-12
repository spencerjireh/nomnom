// HMAC-SHA256 verification for relay requests.
//
// Authorization: NMNM-HMAC-SHA256 <unix_ts>:<hex_mac>
//   mac = HMAC-SHA256(secret, method + "\n" + path + "\n" + unix_ts)
//
// The MAC does NOT cover the request body. Body integrity is provided by the
// AEAD wrapper inside slot_data (nomnom's existing encrypt_bytes adds an HMAC
// over the ciphertext). The relay's HMAC authenticates the client to the
// Worker; it does not vouch for what the client is sending.

import {
  constantTimeEqualHex,
  hmacSha256Hex,
  parseSignedAuth,
  type AuthResult,
} from "./crypto-util";

export type { AuthResult } from "./crypto-util";

const AUTH_PREFIX = "NMNM-HMAC-SHA256 ";

export async function verifyHmac(
  req: Request,
  secret: string,
): Promise<AuthResult> {
  const header = req.headers.get("Authorization") ?? "";
  const parsed = parseSignedAuth(header, AUTH_PREFIX, {
    missing: "missing-mac",
    bad: "bad-mac",
  });
  if (!parsed.ok) return parsed;
  const url = new URL(req.url);
  const msg = `${req.method}\n${url.pathname}\n${parsed.tsStr}`;
  const expected = await hmacSha256Hex(secret, msg);
  if (!constantTimeEqualHex(expected, parsed.macHex)) {
    return { ok: false, status: 401, reason: "bad-mac" };
  }
  return { ok: true };
}
