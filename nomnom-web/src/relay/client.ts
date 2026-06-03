// HTTP client for the relay Worker. Every request carries the HMAC Authorization
// header; the MAC signs (method, bare path, ts). Status semantics match
// nomnom.py's _relay_request / _relay_get_slot.

import { relayAuthHeader } from "../crypto/relay-auth";
import type { RelayConfig } from "../types";
import { RelayError } from "./errors";

export class RelayClient {
  constructor(private readonly config: RelayConfig) {}

  private slotPath(slotId: string): string {
    return `/slots/${slotId}`;
  }

  private url(path: string): string {
    return this.config.url.replace(/\/+$/, "") + path;
  }

  private auth(method: string, path: string): string {
    return relayAuthHeader(this.config.secret, method, path);
  }

  /** Store a payload at a slot. Throws RelayError on 409/413/other non-204. */
  async putSlot(slotId: string, body: ArrayBuffer | Uint8Array, signal?: AbortSignal): Promise<void> {
    const path = this.slotPath(slotId);
    const res = await fetch(this.url(path), {
      method: "PUT",
      headers: { Authorization: this.auth("PUT", path), "Content-Type": "application/octet-stream" },
      // Uint8Array/ArrayBuffer are valid fetch bodies at runtime; the cast works
      // around TS 5.7's generic-ArrayBufferLike narrowing of BodyInit.
      body: body as BodyInit,
      signal,
    });
    if (res.status === 204) return;
    throw new RelayError(res.status, (await safeReason(res)) || "put-failed");
  }

  /**
   * Fetch (and delete) a slot, long-polling up to waitMs. Returns the bytes, or
   * null if the slot was empty for the whole wait (404 = nothing yet). Throws on
   * 410/other errors.
   */
  async getSlot(
    slotId: string,
    opts: { waitMs?: number; signal?: AbortSignal } = {},
  ): Promise<ArrayBuffer | null> {
    const path = this.slotPath(slotId);
    const wait = opts.waitMs ?? 0;
    const res = await fetch(this.url(`${path}?wait=${wait}`), {
      method: "GET",
      headers: { Authorization: this.auth("GET", path) },
      signal: opts.signal,
    });
    if (res.status === 200) return await res.arrayBuffer();
    if (res.status === 404) return null; // nothing in the slot yet
    throw new RelayError(res.status, (await safeReason(res)) || "get-failed");
  }

  /** Best-effort slot deletion (cleanup of authored slots). Never throws. */
  async deleteSlot(slotId: string, signal?: AbortSignal): Promise<void> {
    const path = this.slotPath(slotId);
    try {
      await fetch(this.url(path), {
        method: "DELETE",
        headers: { Authorization: this.auth("DELETE", path) },
        signal,
      });
    } catch {
      // swallow — cleanup is opportunistic
    }
  }

  /** Connectivity probe (no HMAC). */
  async health(signal?: AbortSignal): Promise<boolean> {
    try {
      const res = await fetch(this.url("/health"), { method: "GET", signal });
      return res.ok;
    } catch {
      return false;
    }
  }
}

async function safeReason(res: Response): Promise<string> {
  let text: string;
  try {
    text = (await res.text()).trim();
  } catch {
    return "";
  }
  if (text.startsWith("{")) {
    try {
      const obj = JSON.parse(text);
      if (obj && typeof obj.error === "string") return obj.error;
    } catch {
      // fall through to raw text
    }
  }
  return text;
}
