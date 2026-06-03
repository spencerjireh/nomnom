// Thin wrappers over @noble/hashes so the rest of the crypto module is backend
// agnostic and call sites read like the Python (hashlib/hmac).

import { sha256 as nobleSha256 } from "@noble/hashes/sha2";
import { hmac as nobleHmac, HMAC } from "@noble/hashes/hmac";

export function sha256(...parts: Uint8Array[]): Uint8Array {
  const h = nobleSha256.create();
  for (const p of parts) h.update(p);
  return h.digest();
}

export function hmacSha256(key: Uint8Array, msg: Uint8Array): Uint8Array {
  return nobleHmac(nobleSha256, key, msg);
}

/** A pre-keyed HMAC instance whose inner state can be cloned per message. */
export function hmacSha256Keyed(key: Uint8Array): HMAC<ReturnType<typeof nobleSha256.create>> {
  return nobleHmac.create(nobleSha256, key) as HMAC<ReturnType<typeof nobleSha256.create>>;
}

/** Constant-time equality for two byte arrays (matches hmac.compare_digest). */
export function timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}
