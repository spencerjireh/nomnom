// Diffie-Hellman over RFC 3526 group 14, plus identity generation. Mirrors
// nomnom.py `_dh_keypair` / `_dh_shared` / `_dh_pub_bytes` and the identity shape
// from `_load_identity`.

import { DH_P, DH_G, DH_BYTES } from "./constants";
import { modPow, bytesToBigint, bigintToBytes } from "./bigint";
import { hexToBytes } from "./hex";
import { ikFingerprint } from "./fingerprint";

export interface Identity {
  device_id: string; // 8 random bytes, hex (16 chars)
  name: string;
  ik_priv: string; // format(int, "x")
  ik_pub: string; // format(int, "x")
}

function randomBigintBelow(bound: bigint): bigint {
  // Uniform-enough modular reduction of DH_BYTES of CSPRNG output. Bias over a
  // 2048-bit range drawn from 2048 bits is ~2^-2048 — negligible.
  const bytes = new Uint8Array(DH_BYTES);
  crypto.getRandomValues(bytes);
  return bytesToBigint(bytes) % bound;
}

/** priv = randbelow(P-3) + 2 ; pub = g^priv mod P. Returns [privHex, pubHex]. */
export function dhKeypair(): [string, string] {
  const priv = randomBigintBelow(DH_P - 3n) + 2n;
  const pub = modPow(DH_G, priv, DH_P);
  return [priv.toString(16), pub.toString(16)];
}

/** Shared secret as 256-byte big-endian, validating 2 <= peer <= P-2. */
export function dhSharedBytes(privHex: string, peerPubHex: string): Uint8Array {
  const priv = bytesToBigint(hexToBytes(privHex));
  const peerPub = bytesToBigint(hexToBytes(peerPubHex));
  if (peerPub < 2n || peerPub > DH_P - 2n) {
    throw new Error("invalid DH public value");
  }
  return bigintToBytes(modPow(peerPub, priv, DH_P), DH_BYTES);
}

/** Public value (hex) -> 256-byte big-endian, for the session-key transcript. */
export function dhPubBytes(pubHex: string): Uint8Array {
  return bigintToBytes(bytesToBigint(hexToBytes(pubHex)), DH_BYTES);
}

function randomDeviceId(): string {
  const b = new Uint8Array(8);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

/** Fresh browser identity. Default name `web-<first fingerprint group>`. */
export function generateIdentity(): Identity {
  const [ik_priv, ik_pub] = dhKeypair();
  const shortFp = ikFingerprint(ik_pub).split(":")[0];
  return {
    device_id: randomDeviceId(),
    name: `web-${shortFp}`,
    ik_priv,
    ik_pub,
  };
}
