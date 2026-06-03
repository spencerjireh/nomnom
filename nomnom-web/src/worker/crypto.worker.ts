// Crypto Web Worker. The crypto/ module is imported HERE so its heavy path (the
// stream cipher over big buffers) never runs on the main thread. Each request is
// handled statelessly; the file body is transferred both ways.

/// <reference lib="webworker" />

import { generateIdentity, feedSeal, feedOpen, hexToBytes } from "../crypto";
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
    case "generateIdentity": {
      const p = payload as RequestMessage<"generateIdentity">["payload"];
      return done<"generateIdentity">(generateIdentity(p.name));
    }

    case "feedSeal": {
      const p = payload as RequestMessage<"feedSeal">["payload"];
      const blob = await feedSeal({
        feedKey: hexToBytes(p.feedKeyHex),
        feedId: p.feedId,
        senderMemberId: p.senderMemberId,
        senderSigPrivHex: p.senderSigPrivHex,
        senderSigPubHex: p.senderSigPubHex,
        filename: p.filename,
        body: new Uint8Array(p.data),
        onProgress: (fraction) => progress(id, "xor", fraction),
      });
      // Re-pack into its own ArrayBuffer so the whole backing store isn't transferred.
      const buf = blob.slice().buffer;
      return { result: { blob: buf } satisfies WorkerResults["feedSeal"], transfer: [buf] };
    }

    case "feedOpen": {
      const p = payload as RequestMessage<"feedOpen">["payload"];
      const { header, body } = await feedOpen({
        feedKey: hexToBytes(p.feedKeyHex),
        feedId: p.feedId,
        blob: new Uint8Array(p.blob),
        onProgress: (fraction) => progress(id, "xor", fraction),
      });
      const buf = body.slice().buffer;
      return {
        result: { header, body: buf } satisfies WorkerResults["feedOpen"],
        transfer: [buf],
      };
    }

    default: {
      const _exhaustive: never = op;
      throw new Error(`unknown op: ${String(_exhaustive)}`);
    }
  }
}

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
