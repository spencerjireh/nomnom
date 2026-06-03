// Message protocol between the main thread and the crypto Web Worker.
//
// The 100 MB stream-cipher pass runs in the worker so it never blocks the main
// thread; the main thread sends the already-derived feed key (hex) + a
// transferable ArrayBuffer and gets the result back the same way. Each request
// carries an `id`; the worker replies with zero or more `progress` then exactly
// one `result` or `error`.

import type { Identity } from "../crypto/identity";
import type { FeedHeader } from "../crypto/feeds";

export type ProgressPhase = "xor";

export interface FeedSealReq {
  feedKeyHex: string;
  feedId: string;
  senderMemberId: string;
  senderSigPrivHex: string;
  senderSigPubHex: string;
  filename: string;
  data: ArrayBuffer; // transferred in
}

export interface FeedOpenReq {
  feedKeyHex: string;
  feedId: string;
  blob: ArrayBuffer; // transferred in
}

export interface WorkerRequests {
  generateIdentity: { name?: string };
  feedSeal: FeedSealReq;
  feedOpen: FeedOpenReq;
}

export interface WorkerResults {
  generateIdentity: Identity;
  feedSeal: { blob: ArrayBuffer };
  feedOpen: { header: FeedHeader; body: ArrayBuffer };
}

export type WorkerOp = keyof WorkerRequests;

export interface RequestMessage<Op extends WorkerOp = WorkerOp> {
  id: number;
  op: Op;
  payload: WorkerRequests[Op];
}

export type ResponseMessage =
  | { id: number; kind: "progress"; phase: ProgressPhase; fraction: number }
  | { id: number; kind: "result"; result: unknown }
  | { id: number; kind: "error"; error: string };
