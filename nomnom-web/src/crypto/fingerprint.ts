// Short, readable fingerprint of an identity public key — matches nomnom.py
// `_ik_fingerprint`: first 16 hex chars of sha256(ik_bytes), grouped in 4s,
// ":"-joined. The CLI pads odd-length ik hex with a leading "0" before hashing
// (via hexToBytes here) so browser and CLI fingerprints agree for the same key.

import { hexToBytes } from "./hex";
import { sha256 } from "./primitives";
import { bytesToHexDigest } from "./hex";

export function ikFingerprint(ikHex: string): string {
  let raw: Uint8Array;
  try {
    raw = hexToBytes(ikHex);
  } catch {
    return "?";
  }
  const d = bytesToHexDigest(sha256(raw)).slice(0, 16);
  return [d.slice(0, 4), d.slice(4, 8), d.slice(8, 12), d.slice(12, 16)].join(":");
}
