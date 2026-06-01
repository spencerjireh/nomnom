// BigInt helpers for the 2048-bit DH group. Native `a ** b` does not reduce and
// would build astronomically large intermediates — modular exponentiation must be
// hand-rolled as square-and-multiply. These operate on the client's own secrets
// in-process; matching nomnom.py's (non-constant-time) `pow()` is the bar.

import { DH_BYTES } from "./constants";

/** result = base^exp mod mod, via square-and-multiply. */
export function modPow(base: bigint, exp: bigint, mod: bigint): bigint {
  if (mod === 1n) return 0n;
  let result = 1n;
  base %= mod;
  while (exp > 0n) {
    if (exp & 1n) result = (result * base) % mod;
    base = (base * base) % mod;
    exp >>= 1n;
  }
  return result;
}

/** Big-endian bytes -> bigint. */
export function bytesToBigint(bytes: Uint8Array): bigint {
  let n = 0n;
  for (const b of bytes) n = (n << 8n) | BigInt(b);
  return n;
}

/** bigint -> fixed-width big-endian bytes (default DH_BYTES = 256). Throws if it overflows. */
export function bigintToBytes(n: bigint, width = DH_BYTES): Uint8Array {
  if (n < 0n) throw new Error("cannot serialize negative bigint");
  const out = new Uint8Array(width);
  for (let i = width - 1; i >= 0; i--) {
    out[i] = Number(n & 0xffn);
    n >>= 8n;
  }
  if (n !== 0n) throw new Error("bigint does not fit in width");
  return out;
}
