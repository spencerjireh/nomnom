// Main-thread proxy over the crypto Web Worker. Promise-per-request, keyed by id,
// with progress callbacks for the seal/open ops. `cancel()` terminates the worker
// (killing any in-flight scrypt/XOR) and lazily respawns it — fine because only
// one transfer runs at a time.

import type {
  RequestMessage,
  ResponseMessage,
  WorkerOp,
  WorkerRequests,
  WorkerResults,
  ProgressPhase,
  SealRequest,
  OpenRequest,
} from "./protocol";
import type { Identity } from "../crypto/dh";

type ProgressFn = (phase: ProgressPhase, fraction: number) => void;

interface Pending {
  resolve: (value: unknown) => void;
  reject: (err: Error) => void;
  onProgress?: ProgressFn;
}

class CryptoClient {
  private worker: Worker | null = null;
  private nextId = 1;
  private pending = new Map<number, Pending>();

  private ensureWorker(): Worker {
    if (this.worker) return this.worker;
    const w = new Worker(new URL("./crypto.worker.ts", import.meta.url), { type: "module" });
    w.onmessage = (e: MessageEvent<ResponseMessage>) => this.onMessage(e.data);
    w.onerror = (e) => this.failAll(new Error(e.message || "crypto worker crashed"));
    this.worker = w;
    return w;
  }

  private onMessage(msg: ResponseMessage): void {
    const p = this.pending.get(msg.id);
    if (!p) return;
    if (msg.kind === "progress") {
      p.onProgress?.(msg.phase, msg.fraction);
    } else if (msg.kind === "result") {
      this.pending.delete(msg.id);
      p.resolve(msg.result);
    } else {
      this.pending.delete(msg.id);
      p.reject(new Error(msg.error));
    }
  }

  private failAll(err: Error): void {
    for (const p of this.pending.values()) p.reject(err);
    this.pending.clear();
  }

  private call<Op extends WorkerOp>(
    op: Op,
    payload: WorkerRequests[Op],
    opts: { transfer?: Transferable[]; onProgress?: ProgressFn } = {},
  ): Promise<WorkerResults[Op]> {
    const worker = this.ensureWorker();
    const id = this.nextId++;
    return new Promise<WorkerResults[Op]>((resolve, reject) => {
      this.pending.set(id, {
        resolve: resolve as (v: unknown) => void,
        reject,
        onProgress: opts.onProgress,
      });
      const msg: RequestMessage<Op> = { id, op, payload };
      worker.postMessage(msg, opts.transfer ?? []);
    });
  }

  /** Terminate the worker (cancelling in-flight work) and reject all pending. */
  cancel(): void {
    if (this.worker) {
      this.worker.terminate();
      this.worker = null;
    }
    this.failAll(new Error("cancelled"));
  }

  // --- typed ops ---

  generateIdentity(): Promise<Identity> {
    return this.call("generateIdentity", {});
  }

  ephemeralKeypair(): Promise<{ privHex: string; pubHex: string }> {
    return this.call("ephemeralKeypair", {});
  }

  recurringSlots(
    myIkPrivHex: string,
    theirIkPubHex: string,
  ): Promise<WorkerResults["recurringSlots"]> {
    return this.call("recurringSlots", { myIkPrivHex, theirIkPubHex });
  }

  async recurringBinding(myIkPubHex: string, theirIkPubHex: string): Promise<string> {
    const r = await this.call("recurringBinding", { myIkPubHex, theirIkPubHex });
    return r.bindingHex;
  }

  async firstContactBinding(relaySecret: string): Promise<string> {
    const r = await this.call("firstContactBinding", { relaySecret });
    return r.bindingHex;
  }

  async firstContactInitSlot(bindingHex: string): Promise<string> {
    const r = await this.call("firstContactInitSlot", { bindingHex });
    return r.slot;
  }

  async pairRespSlot(bindingHex: string, ikPubHex: string): Promise<string> {
    const r = await this.call("pairRespSlot", { bindingHex, ikPubHex });
    return r.slot;
  }

  async sealInitiator(req: SealRequest, onProgress?: ProgressFn): Promise<ArrayBuffer> {
    const r = await this.call("sealInitiator", req, { transfer: [req.data], onProgress });
    return r.blob;
  }

  async openResponder(
    req: OpenRequest,
    onProgress?: ProgressFn,
  ): Promise<{ name: string; body: ArrayBuffer }> {
    const r = await this.call("openResponder", req, { transfer: [req.blob], onProgress });
    return r;
  }
}

export const cryptoClient = new CryptoClient();
