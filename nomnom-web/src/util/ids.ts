// Random id / token helpers. Framework-free and pure-ish (crypto RNG only), kept
// out of the React and orchestration layers so they're trivially reusable/testable.

import { bytesToHexDigest } from "../crypto/hex";

/** A timeline-row id: crypto.randomUUID when available, else a cheap fallback. */
export function newId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `e${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

/** secrets.token_urlsafe(n) — n random bytes, url-safe base64, no padding. */
export function randomToken(nBytes: number): string {
  const b = new Uint8Array(nBytes);
  crypto.getRandomValues(b);
  const s = btoa(String.fromCharCode(...b));
  return s.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** n random bytes as fixed-width lowercase hex. */
export function randomHex(nBytes: number): string {
  const b = new Uint8Array(nBytes);
  crypto.getRandomValues(b);
  return bytesToHexDigest(b);
}
