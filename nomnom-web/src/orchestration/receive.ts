// RECEIVE (pinned). Long-poll every pinned peer's recurring init slot in parallel;
// the first to deliver wins and the rest are aborted. Then run the responder half
// of the handshake and decrypt. Mirrors nomnom.py _relay_recv_pinned +
// _relay_recv_complete. One 30s cycle per call — no background polling.

import { RelayClient } from "../relay/client";
import { AuthoredSlots } from "./cleanup";
import { tofuDecision, runTofu } from "./tofu";
import { cryptoClient } from "../worker/cryptoClient";
import { buildHandshakeBlob, parseHandshake, RELAY_INIT_MAGIC, RELAY_RESP_MAGIC } from "../crypto/blobs";
import { RelayError } from "../relay/errors";
import { RELAY_WAIT_MS } from "../config";
import type { Identity, RelayConfig, PeerStore, OnProgress, OnTofu, OnPin } from "../types";

export interface ReceiveParams {
  identity: Identity;
  relay: RelayConfig;
  pins: PeerStore;
  onProgress: OnProgress;
  onTofu: OnTofu;
  onPin: OnPin;
  signal: AbortSignal;
}

export interface ReceiveOutcome {
  name: string;
  body: ArrayBuffer;
  peerName: string;
  bytes: number;
}

class SlotEmpty extends Error {}

interface Winner {
  peerId: string;
  peerIk: string;
  peerName: string;
  slots: { init: string; resp: string; data: string };
  initBuf: ArrayBuffer;
}

export async function runReceivePinned(p: ReceiveParams): Promise<ReceiveOutcome> {
  const { identity, relay, pins, onProgress, onTofu, onPin, signal } = p;
  const peers = Object.entries(pins);
  if (peers.length === 0) throw new Error("no pinned peers. pair a device first.");

  const client = new RelayClient(relay);

  // Resolve each peer's recurring slots up front (one worker round-trip each).
  const targets = await Promise.all(
    peers.map(async ([peerId, pin]) => ({
      peerId,
      peerIk: pin.ik_pub,
      peerName: pin.name,
      slots: await cryptoClient.recurringSlots(identity.ik_priv, pin.ik_pub),
    })),
  );

  onProgress("handshaking", `waiting on ${targets.length} pinned peer(s)`, 0);

  // One controller to cancel the losing long-polls once a winner appears. The
  // external signal (user cancel) also trips it.
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  if (signal.aborted) ctrl.abort();
  else signal.addEventListener("abort", onAbort, { once: true });

  const races = targets.map((t) =>
    client.getSlot(t.slots.init, { waitMs: RELAY_WAIT_MS, signal: ctrl.signal }).then((buf): Winner => {
      if (buf === null) throw new SlotEmpty();
      return { peerId: t.peerId, peerIk: t.peerIk, peerName: t.peerName, slots: t.slots, initBuf: buf };
    }),
  );

  let winner: Winner;
  try {
    winner = await Promise.any(races);
  } catch (err) {
    signal.removeEventListener("abort", onAbort);
    if (signal.aborted) throw new Error("cancelled");
    // Surface a real error (e.g. 401 bad passphrase) over a plain timeout.
    const real = (err as AggregateError).errors?.find(
      (e) => !(e instanceof SlotEmpty) && e?.name !== "AbortError",
    );
    if (real instanceof RelayError) throw real;
    if (real) throw real;
    throw new Error("no transfer (waited 30s).");
  } finally {
    ctrl.abort(); // cancel the losers
    signal.removeEventListener("abort", onAbort);
  }

  return completeReceive(winner, { identity, client, pins, onProgress, onTofu, onPin, signal });
}

async function completeReceive(
  winner: Winner,
  ctx: {
    identity: Identity;
    client: RelayClient;
    pins: PeerStore;
    onProgress: OnProgress;
    onTofu: OnTofu;
    onPin: OnPin;
    signal: AbortSignal;
  },
): Promise<ReceiveOutcome> {
  const { identity, client, pins, onProgress, onTofu, onPin, signal } = ctx;
  const authored = new AuthoredSlots(client);
  try {
    const init = parseHandshake(new Uint8Array(winner.initBuf), RELAY_INIT_MAGIC);
    if (init.device_id !== winner.peerId) {
      throw new Error(
        `recurring rendezvous hit by unexpected peer ${init.device_id} (expected ${winner.peerId}).`,
      );
    }
    // The slots were derived from the pinned peer's IK; trusting a different
    // init.ik would derive the wrong binding/session key and fail to decrypt.
    if (init.ik !== winner.peerIk) {
      throw new Error(`sender identity key does not match the pinned peer ${winner.peerName}.`);
    }

    const decision = tofuDecision(pins, init.device_id, init.ik);
    const ok = await runTofu(
      decision,
      init.device_id,
      init.name,
      init.ik,
      pins[init.device_id]?.ik_pub ?? null,
      onTofu,
    );
    if (!ok) throw new Error("TOFU prompt declined.");

    const ek = await cryptoClient.ephemeralKeypair();
    const binding = await cryptoClient.recurringBinding(identity.ik_pub, init.ik);

    onProgress("handshaking", "answering sender", 0.2);
    const respBlob = buildHandshakeBlob(identity, ek.pubHex, RELAY_RESP_MAGIC);
    await client.putSlot(winner.slots.resp, respBlob, signal);
    authored.add(winner.slots.resp);

    onProgress("transferring", "downloading payload", 0.3);
    const dataBlob = await client.getSlot(winner.slots.data, { waitMs: RELAY_WAIT_MS, signal });
    if (dataBlob === null) throw new Error("sender vanished before sending the payload.");

    onProgress("decrypting", "decrypting", 0.6);
    const { name, body } = await cryptoClient.openResponder(
      {
        myIkPrivHex: identity.ik_priv,
        myEkPrivHex: ek.privHex,
        pubs: {
          ikInitPub: init.ik,
          ekInitPub: init.ek,
          ikRespPub: identity.ik_pub,
          ekRespPub: ek.pubHex,
        },
        bindingHex: binding,
        blob: dataBlob,
      },
      (_phase, fraction) => onProgress("decrypting", "decrypting", 0.6 + 0.4 * fraction),
    );

    onPin({ decision, peerId: init.device_id, peerName: init.name, peerIk: init.ik });
    await authored.cleanup();
    onProgress("done", "done", 1);
    return { name, body, peerName: init.name, bytes: body.byteLength };
  } catch (e) {
    await authored.cleanup();
    throw e;
  }
}
