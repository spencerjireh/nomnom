// Handshake / pair JSON blobs exchanged through relay slots. Mirrors nomnom.py
// `_relay_handshake_blob` / `_relay_pair_blob` and their parsers.
//
// These are JSON-parsed by the peer (not byte-compared), so JSON.stringify field
// ordering is irrelevant for interop — only the field names, types, and hex
// validity matter. The AEAD header is the only thing that must be byte-exact.

import { hexToBytes } from "./hex";
import { RELAY_INIT_MAGIC, RELAY_RESP_MAGIC, RELAY_PAIR_MAGIC } from "./constants";
import type { Identity } from "./dh";

const enc = new TextEncoder();
const dec = new TextDecoder();

export interface HandshakeBlob {
  magic: string;
  ik: string;
  ek: string;
  device_id: string;
  name: string;
}

export interface PairBlob {
  magic: string;
  ik: string;
  device_id: string;
  name: string;
}

export function buildHandshakeBlob(identity: Identity, ekPubHex: string, magic: string): Uint8Array {
  return enc.encode(
    JSON.stringify({
      magic,
      ik: identity.ik_pub,
      ek: ekPubHex,
      device_id: identity.device_id,
      name: identity.name,
    }),
  );
}

export function buildPairBlob(identity: Identity): Uint8Array {
  return enc.encode(
    JSON.stringify({
      magic: RELAY_PAIR_MAGIC,
      ik: identity.ik_pub,
      device_id: identity.device_id,
      name: identity.name,
    }),
  );
}

function parseObject(raw: Uint8Array, what: string): Record<string, unknown> {
  let obj: unknown;
  try {
    obj = JSON.parse(dec.decode(raw));
  } catch (e) {
    throw new Error(`relay returned malformed ${what}: ${e}`);
  }
  if (typeof obj !== "object" || obj === null || Array.isArray(obj)) {
    throw new Error(`relay returned non-object ${what}`);
  }
  return obj as Record<string, unknown>;
}

function requireStr(obj: Record<string, unknown>, key: string, what: string): string {
  const v = obj[key];
  if (typeof v !== "string" || !v) throw new Error(`${what} missing/blank '${key}'`);
  return v;
}

function requireHex(value: string, key: string, what: string): void {
  try {
    hexToBytes(value);
  } catch {
    throw new Error(`${what} field '${key}' is not hex`);
  }
}

export function parseHandshake(raw: Uint8Array, expectMagic: string): HandshakeBlob {
  const obj = parseObject(raw, "handshake");
  if (obj.magic !== expectMagic) {
    throw new Error(`handshake magic mismatch (expected ${expectMagic}, got ${String(obj.magic)})`);
  }
  const blob: HandshakeBlob = {
    magic: expectMagic,
    ik: requireStr(obj, "ik", "handshake"),
    ek: requireStr(obj, "ek", "handshake"),
    device_id: requireStr(obj, "device_id", "handshake"),
    name: requireStr(obj, "name", "handshake"),
  };
  requireHex(blob.ik, "ik", "handshake");
  requireHex(blob.ek, "ek", "handshake");
  return blob;
}

export function parsePairBlob(raw: Uint8Array): PairBlob {
  const obj = parseObject(raw, "pair blob");
  if (obj.magic !== RELAY_PAIR_MAGIC) {
    throw new Error(`pair blob magic mismatch (expected ${RELAY_PAIR_MAGIC}, got ${String(obj.magic)})`);
  }
  const blob: PairBlob = {
    magic: RELAY_PAIR_MAGIC,
    ik: requireStr(obj, "ik", "pair blob"),
    device_id: requireStr(obj, "device_id", "pair blob"),
    name: requireStr(obj, "name", "pair blob"),
  };
  requireHex(blob.ik, "ik", "pair blob");
  return blob;
}

export { RELAY_INIT_MAGIC, RELAY_RESP_MAGIC, RELAY_PAIR_MAGIC };
