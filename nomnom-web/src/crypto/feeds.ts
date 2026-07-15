// Feeds v2 transport crypto — a byte-exact port of nomnom.py's feed_seal /
// feed_open and their helpers. A transfer sealed in the browser must open on a
// CLI Mac and vice-versa, so every byte here is fixed by the CLI.
//
// The user-facing credential is the URL token (host/f/<token>). Every member
// derives the same symmetric feed key from it via HKDF; the Worker authenticates
// requests with an HMAC keyed by that same key. Sender authenticity is separate:
// each post carries an Ed25519 signature over a transcript that binds the feed,
// the sender, the file metadata, and the AEAD nonce (so a signature can't be
// replayed against different ciphertext).

import {
  FEED_MAGIC,
  FEED_NONCE_LEN,
  FEED_MAC_LEN,
  FEED_HEADER_LEN,
  FEED_KEY_SALT,
  FEED_ENC_INFO,
  FEED_MAC_INFO,
  FEED_SIG_DOMAIN,
  FEED_TOKEN_RE,
} from "./constants";
import { hkdf } from "./hkdf";
import { hmacSha256, sha256, timingSafeEqual } from "./primitives";
import { ed25519Sign, ed25519Verify } from "./ed25519";
import { bytesToHexDigest, hexToBytes } from "./hex";
import { streamXorInPlace } from "./stream";

const enc = new TextEncoder();
const dec = new TextDecoder();
const EMPTY = new Uint8Array(0);

/** The decoded, validated header of a feed post. */
export interface FeedHeader {
  v: number;
  fid: string; // feed id
  smid: string; // sender member id
  sik: string; // sender Ed25519 pubkey, hex
  fn: string; // filename
  fs: number; // file size
  ch: string; // sha256(body), hex
  pa: number; // posted-at unix seconds
  sig: string; // Ed25519 signature over the transcript, hex
}

/** Decode URL-safe base64, tolerating missing padding (mirrors the CLI). */
export function b64urlDecodeLoose(s: string): Uint8Array {
  const t = s.trim().replace(/-/g, "+").replace(/_/g, "/");
  const pad = (4 - (t.length % 4)) % 4;
  const bin = atob(t + "=".repeat(pad));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

/**
 * Derive the 32-byte feed key from the URL token. HKDF-SHA256 with
 * salt=`nomnom-feed-v1`, ikm=raw bytes of the token, info=the token string.
 */
export function feedKeyFromToken(token: string): Uint8Array {
  if (!token) throw new Error("feed token must not be empty");
  if (!FEED_TOKEN_RE.test(token)) {
    throw new Error("feed token must be 8-32 url-safe base64 chars");
  }
  const ikm = b64urlDecodeLoose(token);
  return hkdf(FEED_KEY_SALT, ikm, enc.encode(token), 32);
}

/** Derive (encKey, macKey) from the feed key via HKDF-Expand. */
export function feedSubkeys(feedKey: Uint8Array): { encKey: Uint8Array; macKey: Uint8Array } {
  return {
    encKey: hkdf(EMPTY, feedKey, FEED_ENC_INFO, 32),
    macKey: hkdf(EMPTY, feedKey, FEED_MAC_INFO, 32),
  };
}

/** Per-request signature for /feeds/:id/* endpoints (keyed by the feed key). */
export function feedRequestMac(
  feedKey: Uint8Array,
  method: string,
  path: string,
  ts: number,
): string {
  return bytesToHexDigest(hmacSha256(feedKey, enc.encode(`${method}\n${path}\n${ts}`)));
}

function u64be(n: number): Uint8Array {
  const b = new Uint8Array(8);
  new DataView(b.buffer).setBigUint64(0, BigInt(n), false);
  return b;
}

export interface SigTranscriptParams {
  feedId: string;
  senderMemberId: string;
  senderSigPubHex: string;
  filename: string;
  fileSize: number;
  contentHash: Uint8Array;
  postedAt: number;
  nonce: Uint8Array;
}

/** Hash the bound-together fields a sender signs over. Each part is 2-byte-len prefixed. */
export function feedSigTranscript(p: SigTranscriptParams): Uint8Array {
  const parts: Uint8Array[] = [
    FEED_SIG_DOMAIN,
    enc.encode(p.feedId),
    enc.encode(p.senderMemberId),
    hexToBytes(p.senderSigPubHex),
    enc.encode(p.filename),
    u64be(p.fileSize),
    p.contentHash,
    u64be(p.postedAt),
    p.nonce,
  ];
  let total = 0;
  for (const part of parts) total += 2 + part.length;
  const buf = new Uint8Array(total);
  let off = 0;
  for (const part of parts) {
    buf[off++] = (part.length >>> 8) & 0xff;
    buf[off++] = part.length & 0xff;
    buf.set(part, off);
    off += part.length;
  }
  return sha256(buf);
}

/** Pack a JSON header with a 2-byte length prefix (compact, key order fixed). */
export function feedPackHeader(header: Record<string, unknown>): Uint8Array {
  const raw = enc.encode(JSON.stringify(header));
  if (raw.length > 0xffff) throw new Error("feed header too large");
  const out = new Uint8Array(2 + raw.length);
  out[0] = (raw.length >>> 8) & 0xff;
  out[1] = raw.length & 0xff;
  out.set(raw, 2);
  return out;
}

/** Inverse of feedPackHeader. Returns the parsed header and the trailing body. */
export function feedUnpackHeader(plaintext: Uint8Array): { header: unknown; body: Uint8Array } {
  if (plaintext.length < 2) throw new Error("feed plaintext truncated");
  const n = (plaintext[0] << 8) | plaintext[1];
  if (plaintext.length < 2 + n) throw new Error("feed plaintext truncated");
  let header: unknown;
  try {
    header = JSON.parse(dec.decode(plaintext.subarray(2, 2 + n)));
  } catch (e) {
    throw new Error(`feed header is not valid JSON: ${e}`);
  }
  if (typeof header !== "object" || header === null || Array.isArray(header)) {
    throw new Error("feed header is not a JSON object");
  }
  return { header, body: plaintext.subarray(2 + n) };
}

export interface FeedSealParams {
  feedKey: Uint8Array;
  feedId: string;
  senderMemberId: string;
  senderSigPrivHex: string;
  senderSigPubHex: string;
  filename: string;
  body: Uint8Array;
  /** Test/interop hooks; production callers omit them. */
  postedAt?: number;
  nonce?: Uint8Array;
  onProgress?: (fraction: number) => void;
}

/** Encrypt and authenticate a single feed post. */
export async function feedSeal(p: FeedSealParams): Promise<Uint8Array> {
  const nonce = p.nonce ?? randomBytes(FEED_NONCE_LEN);
  const contentHash = sha256(p.body);
  const when = p.postedAt ?? Math.floor(Date.now() / 1000);
  const transcript = feedSigTranscript({
    feedId: p.feedId,
    senderMemberId: p.senderMemberId,
    senderSigPubHex: p.senderSigPubHex,
    filename: p.filename,
    fileSize: p.body.length,
    contentHash,
    postedAt: when,
    nonce,
  });
  const sig = ed25519Sign(transcript, hexToBytes(p.senderSigPrivHex));
  // Key order is fixed by the CLI's json.dumps; V8 preserves literal order.
  const header = {
    v: 1,
    fid: p.feedId,
    smid: p.senderMemberId,
    sik: p.senderSigPubHex,
    fn: p.filename,
    fs: p.body.length,
    ch: bytesToHexDigest(contentHash),
    pa: when,
    sig: bytesToHexDigest(sig),
  };
  const packed = feedPackHeader(header);
  const plaintext = new Uint8Array(packed.length + p.body.length);
  plaintext.set(packed, 0);
  plaintext.set(p.body, packed.length);

  const { encKey, macKey } = feedSubkeys(p.feedKey);
  await streamXorInPlace(encKey, nonce, plaintext, p.onProgress);
  const ciphertext = plaintext;
  const mac = hmacSha256(macKey, concat(FEED_MAGIC, nonce, ciphertext));
  return concat(FEED_MAGIC, nonce, mac, ciphertext);
}

export interface FeedOpenParams {
  feedKey: Uint8Array;
  feedId: string;
  blob: Uint8Array;
  expectMemberId?: string;
  expectSigPubHex?: string;
  onProgress?: (fraction: number) => void;
  /** When true, decrypt the ciphertext in place inside `blob` instead of copying
   * it out first. Only safe when the caller owns `blob` exclusively and won't
   * read it again — e.g. the worker's freshly-transferred, single-owner buffer.
   * Defaults to false: an in-process caller's blob must never be mutated. */
  mutateInPlace?: boolean;
}

/** Verify and decrypt a single feed post. Throws on tamper / bad signature / hash mismatch. */
export async function feedOpen(p: FeedOpenParams): Promise<{ header: FeedHeader; body: Uint8Array }> {
  const { blob } = p;
  if (blob.length < FEED_HEADER_LEN) throw new Error("feed blob too short");
  if (!timingSafeEqual(blob.subarray(0, FEED_MAGIC.length), FEED_MAGIC)) {
    throw new Error("not a feed post (bad magic)");
  }
  let off = FEED_MAGIC.length;
  const nonce = blob.subarray(off, (off += FEED_NONCE_LEN));
  const mac = blob.subarray(off, (off += FEED_MAC_LEN));
  // The in-place XOR (streamXorInPlace) destroys whatever it's handed. By
  // default copy the ciphertext out so the caller's blob is untouched; when the
  // caller owns the buffer exclusively (mutateInPlace), alias it and skip the
  // copy — halving peak memory on the worker path where the blob was just
  // transferred in and is discarded after this call.
  const ciphertext = p.mutateInPlace ? blob.subarray(off) : blob.slice(off);

  const { encKey, macKey } = feedSubkeys(p.feedKey);
  const expected = hmacSha256(macKey, concat(FEED_MAGIC, nonce, ciphertext));
  if (!timingSafeEqual(expected, mac)) throw new Error("feed authentication failed");

  await streamXorInPlace(encKey, nonce, ciphertext, p.onProgress);
  const { header: raw, body } = feedUnpackHeader(ciphertext);
  const h = raw as Record<string, unknown>;
  const v = h.v;
  const fid = h.fid;
  const smid = h.smid;
  const sik = h.sik;
  const fn = h.fn;
  const fs = h.fs;
  const ch = h.ch;
  const pa = h.pa;
  const sigHex = h.sig;
  if (
    typeof v !== "number" ||
    typeof fid !== "string" ||
    typeof smid !== "string" ||
    typeof sik !== "string" ||
    typeof fn !== "string" ||
    typeof fs !== "number" ||
    typeof ch !== "string" ||
    typeof pa !== "number" ||
    typeof sigHex !== "string"
  ) {
    throw new Error("feed header missing or mistyped fields");
  }
  if (fn.includes("/") || fn.includes("\\") || fn === "." || fn === "..") {
    throw new Error(`refusing unsafe filename in feed post: ${fn}`);
  }
  if (fs !== body.length) {
    throw new Error(`feed file_size mismatch: header says ${fs}, body has ${body.length}`);
  }
  const actualHash = sha256(body);
  if (!timingSafeEqual(actualHash, hexToBytes(ch))) {
    throw new Error("feed content_hash mismatch (body corrupted)");
  }
  if (p.expectMemberId !== undefined && smid !== p.expectMemberId) {
    throw new Error("feed sender_member_id mismatch");
  }
  if (p.expectSigPubHex !== undefined && sik !== p.expectSigPubHex) {
    throw new Error("feed sender_sig_pub mismatch");
  }
  const transcript = feedSigTranscript({
    feedId: p.feedId,
    senderMemberId: smid,
    senderSigPubHex: sik,
    filename: fn,
    fileSize: fs,
    contentHash: actualHash,
    postedAt: pa,
    nonce,
  });
  if (!ed25519Verify(transcript, hexToBytes(sigHex), hexToBytes(sik))) {
    throw new Error("feed sender signature failed");
  }
  // Build from validated fields rather than casting `h`, so the returned type
  // can't claim fields the runtime never checked.
  const header: FeedHeader = { v, fid, smid, sik, fn, fs, ch, pa, sig: sigHex };
  return { header, body };
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
