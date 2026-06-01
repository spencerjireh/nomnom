// PAIR (identity-only first contact). Race-decided role: whoever wins the PUT on
// the per-relay rendezvous slot is initiator; the loser is responder. No DH, no
// payload — TOFU + out-of-band fingerprint is the trust gate. Mirrors nomnom.py
// _relay_pair.

import { RelayClient } from "../relay/client";
import { tofuDecision, runTofu } from "./tofu";
import { cryptoClient } from "../worker/cryptoClient";
import { buildPairBlob, parsePairBlob } from "../crypto/blobs";
import { RelayError } from "../relay/errors";
import { RELAY_WAIT_MS } from "../config";
import type {
  Identity,
  RelayConfig,
  PeerStore,
  OnProgress,
  OnTofu,
  OnPin,
  PairResult,
} from "../types";

export interface PairParams {
  identity: Identity;
  relay: RelayConfig;
  pins: PeerStore;
  onProgress: OnProgress;
  onTofu: OnTofu;
  onPin: OnPin;
  signal: AbortSignal;
}

export async function runPair(p: PairParams): Promise<PairResult> {
  const { identity, relay, pins, onProgress, onTofu, onPin, signal } = p;
  const client = new RelayClient(relay);

  const binding = await cryptoClient.firstContactBinding(relay.secret);
  const initSlot = await cryptoClient.firstContactInitSlot(binding);
  const ownBlob = buildPairBlob(identity);

  onProgress("handshaking", "looking for peer", 0);

  // Race for the initiator role by trying to claim the rendezvous slot.
  let isInitiator: boolean;
  try {
    await client.putSlot(initSlot, ownBlob, signal);
    isInitiator = true;
  } catch (e) {
    if (e instanceof RelayError && e.status === 409) isInitiator = false;
    else throw e;
  }

  let peer: { device_id: string; name: string; ik: string };
  let role: "initiator" | "responder";

  if (isInitiator) {
    const respSlot = await cryptoClient.pairRespSlot(binding, identity.ik_pub);
    onProgress("handshaking", "waiting for peer", 0.3);
    let respRaw: ArrayBuffer | null;
    try {
      respRaw = await client.getSlot(respSlot, { waitMs: RELAY_WAIT_MS, signal });
    } catch (e) {
      await client.deleteSlot(initSlot);
      throw e;
    }
    if (respRaw === null) {
      await client.deleteSlot(initSlot);
      throw new Error("no peer connected (waited 30s). run pair on the other device.");
    }
    peer = parsePairBlob(new Uint8Array(respRaw));
    role = "initiator";
  } else {
    onProgress("handshaking", "reading peer identity", 0.3);
    const initRaw = await client.getSlot(initSlot, { waitMs: 0, signal });
    if (initRaw === null) throw new Error("initiator vanished before we could read; retry.");
    peer = parsePairBlob(new Uint8Array(initRaw));
    role = "responder";
  }

  const decision = tofuDecision(pins, peer.device_id, peer.ik);
  onProgress("handshaking", "verifying peer", 0.7);
  const ok = await runTofu(
    decision,
    peer.device_id,
    peer.name,
    peer.ik,
    pins[peer.device_id]?.ik_pub ?? null,
    onTofu,
  );
  if (!ok) {
    if (isInitiator) await client.deleteSlot(initSlot);
    throw new Error("TOFU prompt declined.");
  }

  if (!isInitiator) {
    const respSlot = await cryptoClient.pairRespSlot(binding, peer.ik);
    onProgress("handshaking", "publishing identity", 0.85);
    try {
      await client.putSlot(respSlot, ownBlob, signal);
    } catch (e) {
      if (e instanceof RelayError && e.status === 409) {
        throw new Error("responder slot occupied — another pair beat us; retry.");
      }
      throw e;
    }
  }

  onPin({ decision, peerId: peer.device_id, peerName: peer.name, peerIk: peer.ik });
  onProgress("done", "done", 1);
  return { peerId: peer.device_id, peerName: peer.name, peerIk: peer.ik, role };
}
