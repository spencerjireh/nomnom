// Slot id derivation + session-key bindings. Mirrors nomnom.py `_slot_b64`,
// `_slot_recurring`, `_recurring_binding`, and the first-contact rendezvous slots.

import {
  SLOT_RECURRING_TAG,
  RECURRING_BINDING_TAG,
  FIRST_CONTACT_BINDING_SALT,
  FIRST_CONTACT_RENDEZVOUS_TAG,
  FIRST_CONTACT_RESP_TAG,
  FIRST_CONTACT_SCRYPT_DKLEN,
} from "./constants";
import { sha256, scryptBytes } from "./primitives";
import { dhSharedBytes } from "./dh";
import { hexToBytes } from "./hex";

const enc = new TextEncoder();

/** URL-safe base64, no padding (matches base64.urlsafe_b64encode().rstrip("=")). */
export function slotB64(digest: Uint8Array): string {
  let bin = "";
  for (const b of digest) bin += String.fromCharCode(b);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** Recurring rendezvous base slot for two pinned peers. Suffix with _i/_r/_d. */
export function slotRecurring(myIkPrivHex: string, theirIkPubHex: string): string {
  const shared = dhSharedBytes(myIkPrivHex, theirIkPubHex);
  return slotB64(sha256(SLOT_RECURRING_TAG, shared));
}

export function recurringSlots(myIkPrivHex: string, theirIkPubHex: string) {
  const base = slotRecurring(myIkPrivHex, theirIkPubHex);
  return { base, init: `${base}_i`, resp: `${base}_r`, data: `${base}_d` };
}

function compareBytes(a: Uint8Array, b: Uint8Array): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}

/** Symmetric binding mixed into the recurring session key (raw-byte-sorted concat). */
export function recurringBinding(myIkPubHex: string, theirIkPubHex: string): Uint8Array {
  const a = hexToBytes(myIkPubHex);
  const b = hexToBytes(theirIkPubHex);
  const [first, second] = compareBytes(a, b) < 0 ? [a, b] : [b, a];
  const out = new Uint8Array(RECURRING_BINDING_TAG.length + first.length + second.length);
  out.set(RECURRING_BINDING_TAG, 0);
  out.set(first, RECURRING_BINDING_TAG.length);
  out.set(second, RECURRING_BINDING_TAG.length + first.length);
  return out;
}

/** Per-relay first-contact binding (scrypt over the relay secret). Cache-worthy. */
export function firstContactBinding(relaySecret: string): Promise<Uint8Array> {
  return scryptBytes(enc.encode(relaySecret), FIRST_CONTACT_BINDING_SALT, FIRST_CONTACT_SCRYPT_DKLEN);
}

/** Initiator (sender) rendezvous slot — deterministic per relay secret. */
export function firstContactInitSlot(binding: Uint8Array): string {
  return slotB64(sha256(FIRST_CONTACT_RENDEZVOUS_TAG, binding));
}

/** Responder slot base, keyed by the initiator's identity pubkey. */
export function firstContactRespBase(binding: Uint8Array, senderIkPubHex: string): string {
  return slotB64(sha256(FIRST_CONTACT_RESP_TAG, hexToBytes(senderIkPubHex), binding));
}

/** Pair responder slot = first-contact resp base + "_p". */
export function pairRespSlot(binding: Uint8Array, initiatorIkPubHex: string): string {
  return firstContactRespBase(binding, initiatorIkPubHex) + "_p";
}
