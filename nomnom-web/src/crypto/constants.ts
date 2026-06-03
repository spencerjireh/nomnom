// Protocol constants — copied verbatim from nomnom.py. These must NOT drift; the
// cross-language fixtures in test/ assert byte-for-byte agreement with the CLI.

// --- relay HMAC auth (feed minting) ---
export const RELAY_AUTH_PREFIX = "NMNM-HMAC-SHA256 ";

// --- feeds v2 (NMNF) ---
// Per-post AEAD + Ed25519 sender signature. Wire layout of a sealed post:
// magic(5) | nonce(12) | mac(32) | ciphertext. Keys derive from the URL token
// via HKDF; both ends and the Worker must agree byte-for-byte. (nomnom.py
// _FEED_* constants.)
export const FEED_MAGIC = new Uint8Array([0x4e, 0x4d, 0x4e, 0x46, 0x01]); // b"NMNF\x01"
export const FEED_NONCE_LEN = 12;
export const FEED_MAC_LEN = 32;
export const FEED_HEADER_LEN = FEED_MAGIC.length + FEED_NONCE_LEN + FEED_MAC_LEN; // 49
export const FEED_KEY_SALT = utf8("nomnom-feed-v1");
export const FEED_ENC_INFO = utf8("nomnom-feed-enc");
export const FEED_MAC_INFO = utf8("nomnom-feed-mac");
export const FEED_SIG_DOMAIN = utf8("nomnom-feed-sig-v1");
export const FEED_AUTH_PREFIX = "NMNM-FEEDKEY-SHA256 ";
export const FEED_TOKEN_RE = /^[A-Za-z0-9_-]{8,32}$/;
export const FEED_NICKNAME_RE = /^[a-z0-9][a-z0-9-]{0,31}$/;

function utf8(s: string): Uint8Array {
  return new TextEncoder().encode(s);
}
