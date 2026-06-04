// Main-thread proxy over the crypto Web Worker. Promise-per-request, keyed by id,
// with progress callbacks for seal/open. `cancel()` terminates the worker
// (killing any in-flight XOR) and lazily respawns it — fine because only one
// transfer runs at a time.

import type {
  RequestMessage,
  ResponseMessage,
  WorkerOp,
  WorkerRequests,
  WorkerResults,
  ProgressPhase,
  FeedSealReq,
  FeedOpenReq,
} from "./protocol";
import type { Identity } from "../crypto/identity";

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

  /** Terminate the worker (cancelling in-flight work) and reject all pending.
   * No-op when nothing is in flight — keeps the warm worker around for the
   * next seal/open instead of paying a fresh module-worker boot. */
  cancel(): void {
    if (this.pending.size === 0) return;
    if (this.worker) {
      this.worker.terminate();
      this.worker = null;
    }
    this.failAll(new Error("cancelled"));
  }

  generateIdentity(name?: string): Promise<Identity> {
    return this.call("generateIdentity", { name });
  }

  async feedSeal(req: FeedSealReq, onProgress?: ProgressFn): Promise<ArrayBuffer> {
    const r = await this.call("feedSeal", req, { transfer: [req.data], onProgress });
    return r.blob;
  }

  feedOpen(
    req: FeedOpenReq,
    onProgress?: ProgressFn,
  ): Promise<WorkerResults["feedOpen"]> {
    return this.call("feedOpen", req, { transfer: [req.blob], onProgress });
  }
}

export const cryptoClient = new CryptoClient();
