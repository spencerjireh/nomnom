// Ed25519 sender-authentication keys for feeds v2.
//
// nomnom.py ships a pure-Python RFC 8032 reference implementation; we delegate
// to @noble/curves, which is the same deterministic construction and produces
// byte-identical pubkeys and signatures (verified against the CLI's vectors).
// The private key IS the 32-byte seed — noble expands it via SHA-512 internally,
// exactly like `ed25519_sign` / `ed25519_pub_from_seed`.

import { ed25519 } from "@noble/curves/ed25519";

export interface Ed25519Keypair {
  seed: Uint8Array; // 32-byte private seed
  pub: Uint8Array; // 32-byte public key
}

export function ed25519Keypair(): Ed25519Keypair {
  const seed = new Uint8Array(32);
  crypto.getRandomValues(seed);
  return { seed, pub: ed25519.getPublicKey(seed) };
}

export function ed25519PubFromSeed(seed: Uint8Array): Uint8Array {
  if (seed.length !== 32) throw new Error("ed25519: seed must be 32 bytes");
  return ed25519.getPublicKey(seed);
}

export function ed25519Sign(msg: Uint8Array, seed: Uint8Array): Uint8Array {
  if (seed.length !== 32) throw new Error("ed25519: seed must be 32 bytes");
  return ed25519.sign(msg, seed);
}

/** Verify a 64-byte signature; returns false (never throws) on any malformed input. */
export function ed25519Verify(msg: Uint8Array, sig: Uint8Array, pub: Uint8Array): boolean {
  if (sig.length !== 64 || pub.length !== 32) return false;
  try {
    return ed25519.verify(sig, msg, pub);
  } catch {
    return false;
  }
}
