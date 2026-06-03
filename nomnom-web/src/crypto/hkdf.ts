// HKDF-SHA256 (RFC 5869), a byte-exact port of nomnom.py `_hkdf`.
//
// Extract-then-Expand. An empty salt hashes identically to the Python side:
// HMAC zero-pads its key to the block size, so `salt=b""` and a 32-byte zero
// salt collapse to the same PRK.

import { hmacSha256 } from "./primitives";

export function hkdf(
  salt: Uint8Array,
  ikm: Uint8Array,
  info: Uint8Array,
  length: number,
): Uint8Array {
  if (length > 255 * 32) throw new Error("hkdf: length too large");
  const prk = hmacSha256(salt, ikm);
  const out = new Uint8Array(length);
  let t: Uint8Array = new Uint8Array(0);
  let counter = 1;
  let pos = 0;
  while (pos < length) {
    const input = new Uint8Array(t.length + info.length + 1);
    input.set(t, 0);
    input.set(info, t.length);
    input[t.length + info.length] = counter;
    t = hmacSha256(prk, input);
    const n = Math.min(t.length, length - pos);
    out.set(t.subarray(0, n), pos);
    pos += n;
    counter += 1;
  }
  return out;
}
