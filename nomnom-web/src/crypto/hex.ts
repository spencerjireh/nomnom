// Hex helpers with the exact parity quirks of nomnom.py.
//
// The CLI stores DH keys via `format(int, "x")`, which DROPS a leading zero
// nibble — so identity-key hex can be ODD length. `_hex_to_bytes` pads a leading
// "0" before decoding. Every consumer of key hex (DH parse, bindings, slot
// derivation, fingerprints, blob validation) must do the same or it computes the
// wrong bytes for ~1/16 of keys.

/** Decode hex to bytes, tolerating odd-length input (matches nomnom `_hex_to_bytes`). */
export function hexToBytes(hex: string): Uint8Array {
  const h = hex.length % 2 === 0 ? hex : "0" + hex;
  if (!/^[0-9a-fA-F]*$/.test(h)) throw new Error("invalid hex");
  const out = new Uint8Array(h.length / 2);
  for (let i = 0; i < out.length; i++) {
    out[i] = Number.parseInt(h.slice(i * 2, i * 2 + 2), 16);
  }
  return out;
}

/**
 * Plain fixed-width lowercase hex (never strips). Use for digests like
 * `session_key.hex()` (always 32 bytes -> 64 chars) which is fed to scrypt as the
 * AEAD passphrase — a digest's leading zero byte is significant and must survive.
 */
export function bytesToHexDigest(bytes: Uint8Array): string {
  let s = "";
  for (const b of bytes) s += b.toString(16).padStart(2, "0");
  return s;
}
