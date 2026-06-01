// SEND (recurring, to a pinned peer). Three-message handshake over the relay:
// PUT init -> long-poll resp -> seal -> PUT ciphertext. Mirrors nomnom.py
// _relay_send.

import { RelayClient } from "../relay/client";
import { AuthoredSlots } from "./cleanup";
import { tofuDecision, runTofu } from "./tofu";
import { cryptoClient } from "../worker/cryptoClient";
import { buildHandshakeBlob, parseHandshake, RELAY_INIT_MAGIC, RELAY_RESP_MAGIC } from "../crypto/blobs";
import { RelayError } from "../relay/errors";
import { RELAY_WAIT_MS, MAX_PAYLOAD_BYTES } from "../config";
import type {
  Identity,
  RelayConfig,
  PeerStore,
  OnProgress,
  OnTofu,
  OnPin,
  TransferResult,
} from "../types";

export interface SendParams {
  identity: Identity;
  relay: RelayConfig;
  pins: PeerStore;
  target: { peerId: string; peerIk: string; peerName: string };
  payload: { name: string; data: ArrayBuffer };
  onProgress: OnProgress;
  onTofu: OnTofu;
  onPin: OnPin;
  signal: AbortSignal;
}

export async function runSend(p: SendParams): Promise<TransferResult> {
  const { identity, relay, pins, target, payload, onProgress, onTofu, onPin, signal } = p;
  const byteLength = payload.data.byteLength; // capture before the buffer is transferred
  if (byteLength > MAX_PAYLOAD_BYTES) {
    throw new Error(`payload too large for relay (${byteLength} bytes; limit ${MAX_PAYLOAD_BYTES})`);
  }

  const client = new RelayClient(relay);
  const authored = new AuthoredSlots(client);

  try {
    onProgress("handshaking", "uploading handshake", 0);
    const slots = await cryptoClient.recurringSlots(identity.ik_priv, target.peerIk);
    const binding = await cryptoClient.recurringBinding(identity.ik_pub, target.peerIk);
    const ek = await cryptoClient.ephemeralKeypair();
    const initBlob = buildHandshakeBlob(identity, ek.pubHex, RELAY_INIT_MAGIC);

    try {
      await client.putSlot(slots.init, initBlob, signal);
    } catch (e) {
      if (e instanceof RelayError && e.status === 409) {
        throw new Error("a transfer to this peer is already in progress; retry shortly.");
      }
      throw e;
    }
    authored.add(slots.init);

    onProgress("handshaking", "waiting for receiver", 0.1);
    const respBlob = await client.getSlot(slots.resp, { waitMs: RELAY_WAIT_MS, signal });
    if (respBlob === null) throw new Error("receiver didn't connect (waited 30s).");
    const resp = parseHandshake(new Uint8Array(respBlob), RELAY_RESP_MAGIC);

    // The recurring slots + binding were derived from the pinned target's IK, so a
    // legitimate responder on this slot IS that peer. Reject a mismatch before
    // deriving keys — otherwise we'd seal against the wrong identity, re-pin, and
    // report success even though the receiver can't open the payload.
    if (resp.device_id !== target.peerId || resp.ik !== target.peerIk) {
      throw new Error(`responder identity does not match the pinned target ${target.peerName}.`);
    }

    const decision = tofuDecision(pins, resp.device_id, resp.ik);
    const ok = await runTofu(
      decision,
      resp.device_id,
      resp.name,
      resp.ik,
      pins[resp.device_id]?.ik_pub ?? null,
      onTofu,
    );
    if (!ok) throw new Error("TOFU prompt declined.");

    onProgress("encrypting", "encrypting", 0);
    const blob = await cryptoClient.sealInitiator(
      {
        myIkPrivHex: identity.ik_priv,
        myEkPrivHex: ek.privHex,
        pubs: {
          ikInitPub: identity.ik_pub,
          ekInitPub: ek.pubHex,
          ikRespPub: resp.ik,
          ekRespPub: resp.ek,
        },
        bindingHex: binding,
        name: payload.name,
        data: payload.data,
      },
      (_phase, fraction) => onProgress("encrypting", "encrypting", fraction),
    );

    onProgress("transferring", "uploading payload", 0.9);
    // The ciphertext slot is NOT authored: the receiver's GET deletes it on read,
    // and any orphan is collected by the Worker's 5-min TTL.
    await client.putSlot(slots.data, blob, signal);

    onPin({ decision, peerId: resp.device_id, peerName: resp.name, peerIk: resp.ik });
    await authored.cleanup();
    onProgress("done", "done", 1);
    return { name: payload.name, bytes: byteLength, peerName: resp.name };
  } catch (e) {
    await authored.cleanup();
    throw e;
  }
}
