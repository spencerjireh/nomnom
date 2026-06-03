// This device's nomnom identity — an Ed25519 keypair plus metadata. Shape
// matches the CLI's identity.json so a browser device is just another nomnom
// device: device_id (8 random bytes), name, and the sig keypair (32-byte seed +
// 32-byte pubkey, both hex). The seed is the Ed25519 private key.

import { ed25519Keypair } from "./ed25519";
import { bytesToHexDigest } from "./hex";

export interface Identity {
  device_id: string; // 16 hex chars
  name: string;
  sig_priv: string; // 64 hex chars (32-byte seed)
  sig_pub: string; // 64 hex chars
}

export function generateIdentity(name?: string): Identity {
  const { seed, pub } = ed25519Keypair();
  const dev = new Uint8Array(8);
  crypto.getRandomValues(dev);
  return {
    device_id: bytesToHexDigest(dev),
    name: name && name.trim() ? name.trim() : "web-guest",
    sig_priv: bytesToHexDigest(seed),
    sig_pub: bytesToHexDigest(pub),
  };
}
