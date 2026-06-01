// Protocol constants — copied verbatim from nomnom.py. These must NOT drift; the
// cross-language fixtures in test/ assert byte-for-byte agreement with the CLI.

// RFC 3526 group 14 (2048-bit MODP prime), generator g = 2. Long-term identity
// keys and per-transfer ephemerals both live in this group. (nomnom.py _DH_P/_DH_G)
export const DH_PRIME_HEX =
  "FFFFFFFFFFFFFFFFC90FDAA22168C234C4C6628B80DC1CD1" +
  "29024E088A67CC74020BBEA63B139B22514A08798E3404DD" +
  "EF9519B3CD3A431B302B0A6DF25F14374FE1356D6D51C245" +
  "E485B576625E7EC6F44C42E9A637ED6B0BFF5CB6F406B7ED" +
  "EE386BFB5A899FA5AE9F24117C4B1FE649286651ECE45B3D" +
  "C2007CB8A163BF0598DA48361C55D39A69163FA8FD24CF5F" +
  "83655D23DCA3AD961C62F356208552BB9ED529077096966D" +
  "670C354E4ABC9804F1746C08CA18217C32905E462E36CE3B" +
  "E39E772C180E86039B2783A2EC07A28FB5C55DF06F4C52C9" +
  "DE2BCBF6955817183995497CEA956AE515D2261898FA0510" +
  "15728E5A8AACAA68FFFFFFFFFFFFFFFF";

export const DH_P = BigInt("0x" + DH_PRIME_HEX);
export const DH_G = 2n;
// (P.bit_length() + 7) // 8 — every DH public value is serialized to this width.
export const DH_BYTES = 256;

// --- AEAD (seal_bytes / open_bytes) ---
export const NMNM_MAGIC = new Uint8Array([0x4e, 0x4d, 0x4e, 0x4d, 0x01]); // b"NMNM\x01"
export const NMNM_SALT_LEN = 16;
export const NMNM_NONCE_LEN = 12;
export const NMNM_MAC_LEN = 32;
export const NMNM_HEADER_LEN =
  NMNM_MAGIC.length + NMNM_SALT_LEN + NMNM_NONCE_LEN + NMNM_MAC_LEN; // 64

// scrypt for the AEAD key (dkLen 64 = 32 enc + 32 mac). maxmem is omitted: it's a
// CPython validation guard with no effect on output, and noble's default (1 GB)
// comfortably covers N=2^16,r=8 (~64 MiB).
export const SCRYPT_N = 2 ** 16;
export const SCRYPT_R = 8;
export const SCRYPT_P = 1;
export const SCRYPT_KEY_LEN = 64;

// --- session key transcript ---
export const SESSION_TAG = utf8("nomnom-session-v1");
export const SESSION_BIND_PREFIX = new Uint8Array([0x00, ...utf8("bind:")]); // b"\x00bind:"

// --- slot derivation + bindings ---
export const SLOT_RECURRING_TAG = utf8("nomnom-peer-rendezvous-v1");
export const RECURRING_BINDING_TAG = utf8("recurring-v1");

export const FIRST_CONTACT_BINDING_SALT = utf8("nomnom-first-contact-v2");
export const FIRST_CONTACT_RENDEZVOUS_TAG = utf8("nomnom-rendezvous-v1");
export const FIRST_CONTACT_RESP_TAG = utf8("nomnom-rendezvous-resp-v1");
// First-contact binding scrypt: same N/r/p as the AEAD KDF, dkLen 32.
export const FIRST_CONTACT_SCRYPT_DKLEN = 32;

// --- relay HMAC auth ---
export const RELAY_AUTH_PREFIX = "NMNM-HMAC-SHA256 ";

// --- handshake / pair blob magics ---
export const RELAY_INIT_MAGIC = "nomnom-init-v1";
export const RELAY_RESP_MAGIC = "nomnom-resp-v1";
export const RELAY_PAIR_MAGIC = "nomnom-pair-v1";

function utf8(s: string): Uint8Array {
  return new TextEncoder().encode(s);
}
