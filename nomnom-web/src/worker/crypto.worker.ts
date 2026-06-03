// Crypto Web Worker. The crypto/ module is imported HERE ONLY so its heavy paths
// (scrypt, the stream cipher over big buffers, modexp) never run on the main
// thread. Each request is handled statelessly; buffers are transferred both ways.

/// <reference lib="webworker" />

import {
  generateIdentity,
  dhKeypair,
  recurringSlots,
  recurringBinding,
  firstContactBinding,
  firstContactInitSlot,
  pairRespSlot,
  sessionKeyInitiator,
  sessionKeyResponder,
  sealBytesWithRandom,
  openBytes,
  bytesToHexDigest,
  hexToBytes,
} from "../crypto";
import type {
  RequestMessage,
  ResponseMessage,
  WorkerOp,
  WorkerResults,
  ProgressPhase,
} from "./protocol";

const ctx = self as unknown as DedicatedWorkerGlobalScope;

function post(msg: ResponseMessage, transfer?: Transferable[]): void {
  ctx.postMessage(msg, transfer ?? []);
}

function progress(id: number, phase: ProgressPhase, fraction: number): void {
  post({ id, kind: "progress", phase, fraction });
}

async function handle(req: RequestMessage): Promise<{ result: unknown; transfer: Transferable[] }> {
  const { id, op, payload } = req;
  switch (op) {
    case "generateIdentity":
      return done<"generateIdentity">(generateIdentity());

    case "ephemeralKeypair": {
      const [privHex, pubHex] = dhKeypair();
      return done<"ephemeralKeypair">({ privHex, pubHex });
    }

    case "recurringSlots": {
      const p = payload as RequestMessage<"recurringSlots">["payload"];
      const s = recurringSlots(p.myIkPrivHex, p.theirIkPubHex);
      return done<"recurringSlots">(s);
    }

    case "recurringBinding": {
      const p = payload as RequestMessage<"recurringBinding">["payload"];
      const b = recurringBinding(p.myIkPubHex, p.theirIkPubHex);
      return done<"recurringBinding">({ bindingHex: bytesToHexDigest(b) });
    }

    case "firstContactBinding": {
      const p = payload as RequestMessage<"firstContactBinding">["payload"];
      const b = await firstContactBinding(p.relaySecret);
      return done<"firstContactBinding">({ bindingHex: bytesToHexDigest(b) });
    }

    case "firstContactInitSlot": {
      const p = payload as RequestMessage<"firstContactInitSlot">["payload"];
      return done<"firstContactInitSlot">({ slot: firstContactInitSlot(hexToBytes(p.bindingHex)) });
    }

    case "pairRespSlot": {
      const p = payload as RequestMessage<"pairRespSlot">["payload"];
      return done<"pairRespSlot">({ slot: pairRespSlot(hexToBytes(p.bindingHex), p.ikPubHex) });
    }

    case "sealInitiator": {
      const p = payload as RequestMessage<"sealInitiator">["payload"];
      const key = sessionKeyInitiator(p.myIkPrivHex, p.myEkPrivHex, p.pubs, hexToBytes(p.bindingHex));
      const blob = await sealBytesWithRandom(
        new Uint8Array(p.data),
        p.name,
        bytesToHexDigest(key),
        { onProgress: (phase, fraction) => progress(id, phase, fraction) },
      );
      // Re-pack into its own ArrayBuffer so the whole (possibly larger) backing
      // store isn't transferred.
      const buf = blob.slice().buffer;
      return { result: { blob: buf } satisfies WorkerResults["sealInitiator"], transfer: [buf] };
    }

    case "openResponder": {
      const p = payload as RequestMessage<"openResponder">["payload"];
      const key = sessionKeyResponder(p.myIkPrivHex, p.myEkPrivHex, p.pubs, hexToBytes(p.bindingHex));
      const { name, body } = await openBytes(new Uint8Array(p.blob), bytesToHexDigest(key), {
        onProgress: (phase, fraction) => progress(id, phase, fraction),
      });
      const buf = body.slice().buffer;
      return {
        result: { name, body: buf } satisfies WorkerResults["openResponder"],
        transfer: [buf],
      };
    }

    default: {
      const _exhaustive: never = op;
      throw new Error(`unknown op: ${String(_exhaustive)}`);
    }
  }
}

// Small helper to satisfy the result typing for buffer-less ops.
function done<Op extends WorkerOp>(result: WorkerResults[Op]): {
  result: unknown;
  transfer: Transferable[];
} {
  return { result, transfer: [] };
}

ctx.onmessage = async (e: MessageEvent<RequestMessage>) => {
  const req = e.data;
  try {
    const { result, transfer } = await handle(req);
    post({ id: req.id, kind: "result", result }, transfer);
  } catch (err) {
    post({ id: req.id, kind: "error", error: err instanceof Error ? err.message : String(err) });
  }
};
