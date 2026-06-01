// Thin wrappers over @noble/hashes so the rest of the crypto module is backend
// agnostic and call sites read like the Python (hashlib/hmac/scrypt).

import { sha256 as nobleSha256 } from "@noble/hashes/sha2";
import { hmac as nobleHmac, HMAC } from "@noble/hashes/hmac";
import { scryptAsync } from "@noble/hashes/scrypt";
import {
  SCRYPT_N,
  SCRYPT_R,
  SCRYPT_P,
  SCRYPT_KEY_LEN,
} from "./constants";

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

/** scrypt with nomnom's AEAD parameters (N=2^16, r=8, p=1, dkLen=64). */
export function deriveAeadKey(
  passphrase: Uint8Array,
  salt: Uint8Array,
  onProgress?: (fraction: number) => void,
): Promise<Uint8Array> {
  return scryptAsync(passphrase, salt, {
    N: SCRYPT_N,
    r: SCRYPT_R,
    p: SCRYPT_P,
    dkLen: SCRYPT_KEY_LEN,
    onProgress,
  });
}

/** scrypt with an explicit dkLen (used for the first-contact binding, dkLen=32). */
export function scryptBytes(
  passphrase: Uint8Array,
  salt: Uint8Array,
  dkLen: number,
): Promise<Uint8Array> {
  return scryptAsync(passphrase, salt, {
    N: SCRYPT_N,
    r: SCRYPT_R,
    p: SCRYPT_P,
    dkLen,
  });
}

/** Constant-time equality for two byte arrays (matches hmac.compare_digest). */
export function timingSafeEqual(a: Uint8Array, b: Uint8Array): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i];
  return diff === 0;
}
