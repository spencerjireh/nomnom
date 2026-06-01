// Authenticated encryption, a byte-exact port of nomnom.py seal_bytes/open_bytes.
//
// Construction: scrypt -> enc_key||mac_key; keystream = HMAC-SHA256(enc_key,
// nonce||counterBE32) per 32-byte block; encrypt-then-MAC over magic|salt|nonce|
// ciphertext. Wire layout: magic(5) | salt(16) | nonce(12) | mac(32) | ciphertext.
//
// The wire format is fixed by the CLI, so the only performance levers are how we
// compute it (clone a pre-keyed HMAC, word-wise XOR, chunked yields) — never what
// we compute.

import {
  NMNM_MAGIC,
  NMNM_SALT_LEN,
  NMNM_NONCE_LEN,
  NMNM_MAC_LEN,
  NMNM_HEADER_LEN,
} from "./constants";
import {
  deriveAeadKey,
  hmacSha256,
  hmacSha256Keyed,
  timingSafeEqual,
} from "./primitives";

const BLOCK = 32; // HMAC-SHA256 output size
const CHUNK_BLOCKS = 131072; // 4 MiB per progress tick / yield

export interface AeadProgress {
  // Two phases mirror the cost: scrypt (key derivation) then xor (stream cipher).
  onProgress?: (phase: "scrypt" | "xor", fraction: number) => void;
}

const enc = new TextEncoder();

/** json.dumps({"name": name, "v": 1}, ensure_ascii=False) — exact bytes. */
function packPayload(name: string, body: Uint8Array): Uint8Array {
  // Python default separators are ", " and ": "; key order is name then v.
  // JSON.stringify(name) reproduces Python's string escaping under
  // ensure_ascii=False (same control-char rules, non-ASCII left raw).
  const headerStr = `{"name": ${JSON.stringify(name)}, "v": 1}`;
  const header = enc.encode(headerStr);
  if (header.length > 0xffff) throw new Error("payload header too large");
  const out = new Uint8Array(2 + header.length + body.length);
  out[0] = (header.length >>> 8) & 0xff;
  out[1] = header.length & 0xff;
  out.set(header, 2);
  out.set(body, 2 + header.length);
  return out;
}

function unpackPayload(payload: Uint8Array): { name: string; body: Uint8Array } {
  if (payload.length < 2) throw new Error("payload truncated");
  const headerLen = (payload[0] << 8) | payload[1];
  if (payload.length < 2 + headerLen) throw new Error("payload truncated");
  let header: unknown;
  try {
    header = JSON.parse(new TextDecoder().decode(payload.subarray(2, 2 + headerLen)));
  } catch (e) {
    throw new Error(`payload header is not valid JSON: ${e}`);
  }
  const name = (header as { name?: unknown })?.name;
  if (typeof name !== "string" || !name) throw new Error("payload header missing 'name'");
  if (name.includes("/") || name.includes("\\") || name === "." || name === "..") {
    throw new Error(`refusing unsafe name in payload: ${name}`);
  }
  return { name, body: payload.subarray(2 + headerLen) };
}

/**
 * XOR `buf` in place with the HMAC keystream. Cloning a pre-keyed HMAC avoids
 * re-absorbing the 32-byte key on every one of (len/32) blocks. Yields a
 * microtask every CHUNK_BLOCKS so a 100 MB pass doesn't starve the worker's
 * message queue, and reports fractional progress.
 */
async function streamXorInPlace(
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
    // counter as 4-byte big-endian, appended after the nonce.
    msg[nonce.length] = (counter >>> 24) & 0xff;
    msg[nonce.length + 1] = (counter >>> 16) & 0xff;
    msg[nonce.length + 2] = (counter >>> 8) & 0xff;
    msg[nonce.length + 3] = counter & 0xff;
    const ks = keyed.clone().update(msg).digest();

    const end = Math.min(off + BLOCK, buf.length);
    const n = end - off;
    if (n === BLOCK && (buf.byteOffset + off) % 4 === 0) {
      // Word-wise XOR for full, aligned blocks (8 uint32).
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

async function deriveKeys(
  passphrase: string,
  salt: Uint8Array,
  onScrypt?: (f: number) => void,
): Promise<{ encKey: Uint8Array; macKey: Uint8Array }> {
  const dk = await deriveAeadKey(enc.encode(passphrase), salt, onScrypt);
  return { encKey: dk.subarray(0, 32), macKey: dk.subarray(32) };
}

export interface SealOpts extends AeadProgress {
  salt?: Uint8Array; // test hook
  nonce?: Uint8Array; // test hook
}

/** Encrypt `data` under `passphrase`, embedding `name`. Returns the wire blob. */
export async function sealBytes(
  data: Uint8Array,
  name: string,
  passphrase: string,
  opts: SealOpts = {},
): Promise<Uint8Array> {
  if (!passphrase) throw new Error("passphrase must not be empty");
  const salt = opts.salt ?? randomBytes(NMNM_SALT_LEN);
  const nonce = opts.nonce ?? randomBytes(NMNM_NONCE_LEN);
  const { encKey, macKey } = await deriveKeys(passphrase, salt, (f) =>
    opts.onProgress?.("scrypt", f),
  );
  const payload = packPayload(name, data);
  // XOR in place: payload is freshly allocated, we own it.
  await streamXorInPlace(encKey, nonce, payload, (f) => opts.onProgress?.("xor", f));
  const ciphertext = payload;

  const macInput = concat(NMNM_MAGIC, salt, nonce, ciphertext);
  const mac = hmacSha256(macKey, macInput);

  return concat(NMNM_MAGIC, salt, nonce, mac, ciphertext);
}

/** Verify and decrypt a blob from sealBytes. Throws on tamper / wrong passphrase. */
export async function openBytes(
  blob: Uint8Array,
  passphrase: string,
  opts: AeadProgress = {},
): Promise<{ name: string; body: Uint8Array }> {
  if (blob.length < NMNM_HEADER_LEN) throw new Error("ciphertext too short");
  if (!timingSafeEqual(blob.subarray(0, NMNM_MAGIC.length), NMNM_MAGIC)) {
    throw new Error("not a nomnom-encrypted file (bad magic)");
  }
  let off = NMNM_MAGIC.length;
  const salt = blob.subarray(off, (off += NMNM_SALT_LEN));
  const nonce = blob.subarray(off, (off += NMNM_NONCE_LEN));
  const mac = blob.subarray(off, (off += NMNM_MAC_LEN));
  // Copy the ciphertext so we can XOR it in place without mutating the caller's blob.
  const ciphertext = blob.slice(off);

  const { encKey, macKey } = await deriveKeys(passphrase, salt, (f) =>
    opts.onProgress?.("scrypt", f),
  );
  const expected = hmacSha256(macKey, concat(NMNM_MAGIC, salt, nonce, ciphertext));
  if (!timingSafeEqual(expected, mac)) throw new Error("authentication failed");

  await streamXorInPlace(encKey, nonce, ciphertext, (f) => opts.onProgress?.("xor", f));
  return unpackPayload(ciphertext);
}

function randomBytes(n: number): Uint8Array {
  const b = new Uint8Array(n);
  crypto.getRandomValues(b);
  return b;
}

function concat(...parts: Uint8Array[]): Uint8Array {
  let len = 0;
  for (const p of parts) len += p.length;
  const out = new Uint8Array(len);
  let off = 0;
  for (const p of parts) {
    out.set(p, off);
    off += p.length;
  }
  return out;
}

// Exposed for unit testing the exact header bytes.
export const _internal = { packPayload, unpackPayload, streamXorInPlace };
