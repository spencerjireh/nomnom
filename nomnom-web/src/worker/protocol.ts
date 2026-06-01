// Message protocol between the main thread and the crypto Web Worker.
//
// Heavy or memory-bound crypto (scrypt, the 100 MB stream-cipher pass, 2048-bit
// modexp) runs in the worker; the main thread sends hex keys + transferable
// ArrayBuffers and gets results back the same way. Each request carries an `id`;
// the worker replies with one `progress`* then exactly one `result` or `error`.

import type { Identity } from "../crypto/dh";
import type { Pubs } from "../crypto/session";

export type ProgressPhase = "scrypt" | "xor";

export interface SealRequest {
  myIkPrivHex: string;
  myEkPrivHex: string;
  pubs: Pubs;
  bindingHex: string;
  name: string;
  data: ArrayBuffer; // transferred in
}

export interface OpenRequest {
  myIkPrivHex: string;
  myEkPrivHex: string;
  pubs: Pubs;
  bindingHex: string;
  blob: ArrayBuffer; // transferred in
}

// Request payloads keyed by op name.
export interface WorkerRequests {
  generateIdentity: Record<string, never>;
  ephemeralKeypair: Record<string, never>;
  recurringSlots: { myIkPrivHex: string; theirIkPubHex: string };
  recurringBinding: { myIkPubHex: string; theirIkPubHex: string };
  firstContactBinding: { relaySecret: string };
  firstContactInitSlot: { bindingHex: string };
  pairRespSlot: { bindingHex: string; ikPubHex: string };
  sealInitiator: SealRequest;
  openResponder: OpenRequest;
}

// Result payloads keyed by op name.
export interface WorkerResults {
  generateIdentity: Identity;
  ephemeralKeypair: { privHex: string; pubHex: string };
  recurringSlots: { base: string; init: string; resp: string; data: string };
  recurringBinding: { bindingHex: string };
  firstContactBinding: { bindingHex: string };
  firstContactInitSlot: { slot: string };
  pairRespSlot: { slot: string };
  sealInitiator: { blob: ArrayBuffer };
  openResponder: { name: string; body: ArrayBuffer };
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
