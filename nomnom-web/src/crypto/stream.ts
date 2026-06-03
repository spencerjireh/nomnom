// The nomnom keystream: HMAC-SHA256(encKey, nonce || counterBE32) per 32-byte
// block, XORed into the buffer. Shared by the feeds AEAD (feed_seal/feed_open).
//
// The construction is fixed by the CLI; the only levers are how we compute it —
// clone a pre-keyed HMAC (avoids re-absorbing the key per block), word-wise XOR
// for aligned full blocks, and a microtask yield every CHUNK_BLOCKS so a 100 MB
// pass doesn't starve the worker's message queue.

import { hmacSha256Keyed } from "./primitives";

const BLOCK = 32; // HMAC-SHA256 output size
const CHUNK_BLOCKS = 131072; // 4 MiB per progress tick / yield

/** XOR `buf` in place with the keystream, reporting fractional progress. */
export async function streamXorInPlace(
  encKey: Uint8Array,
  nonce: Uint8Array,
  buf: Uint8Array,
  onProgress?: (fraction: number) => void,
): Promise<void> {
  const keyed = hmacSha256Keyed(encKey);
  const msg = new Uint8Array(nonce.length + 4);
  msg.set(nonce, 0);
  const total = Math.ceil(buf.length / BLOCK);
  let counter = 0;
  for (let off = 0; off < buf.length; off += BLOCK) {
    msg[nonce.length] = (counter >>> 24) & 0xff;
    msg[nonce.length + 1] = (counter >>> 16) & 0xff;
    msg[nonce.length + 2] = (counter >>> 8) & 0xff;
    msg[nonce.length + 3] = counter & 0xff;
    const ks = keyed.clone().update(msg).digest();

    const end = Math.min(off + BLOCK, buf.length);
    const n = end - off;
    if (n === BLOCK && (buf.byteOffset + off) % 4 === 0) {
      const bufW = new Uint32Array(buf.buffer, buf.byteOffset + off, 8);
      const ksW = new Uint32Array(ks.buffer, ks.byteOffset, 8);
      for (let i = 0; i < 8; i++) bufW[i] ^= ksW[i];
    } else {
      for (let i = 0; i < n; i++) buf[off + i] ^= ks[i];
    }

    counter++;
    if (counter % CHUNK_BLOCKS === 0) {
      onProgress?.(counter / total);
      await Promise.resolve();
    }
  }
  onProgress?.(1);
}
