// HTTP client for the relay Worker's /feeds/* endpoints. Status semantics mirror
// nomnom.py's _relay_mint_feed / _relay_* feed helpers.
//
// Two auth schemes:
//   - mintFeed (POST /feeds) is gated by the deployment-wide relay HMAC secret —
//     only your relay's users can create feeds.
//   - every /feeds/:id/* request is signed with the per-feed key derived from the
//     URL token (see crypto/feed-auth). Possession of the URL grants access to
//     that feed alone.
//
// The query string is appended to the fetched URL but stripped before signing
// (the Worker signs over the bare pathname).

import { feedAuthHeader } from "../crypto/feed-auth";
import { feedRequestMac } from "../crypto/feeds";
import { relayAuthHeader } from "../crypto/relay-auth";
import { STREAM_RECONNECT_MS, STREAM_UNSUPPORTED_RETRIES } from "../config";
import { sleep } from "../util/sleep";
import type { Member, RelayConfig } from "../types";
import { RelayError, StreamUnsupportedError } from "./errors";

export interface MintResult {
  feed_id: string;
  expires_at: number;
  created_at: number;
}

export interface SlotMeta {
  slot_id: string;
  created_at: number;
}

/**
 * `?wait=..&since=..` — drop only `undefined`. `wait=0` is dropped because the
 * Worker treats absent and 0 identically (no long-poll). `since=0` is a real
 * "from the beginning" cursor that the Worker accepts (>= 0). Order is built
 * explicitly (wait, then since); the Worker strips the query before signing, so
 * order is cosmetic, not load-bearing.
 */
function qs(params: { wait?: number; since?: number }): string {
  const parts: string[] = [];
  if (params.wait !== undefined && params.wait > 0) {
    parts.push(`wait=${Math.floor(params.wait)}`);
  }
  if (params.since !== undefined && params.since >= 0) {
    parts.push(`since=${Math.floor(params.since)}`);
  }
  return parts.length ? "?" + parts.join("&") : "";
}

function stripTrailingSlash(u: string): string {
  return u.replace(/\/+$/, "");
}

/**
 * Mint a new feed. HMAC-gated by the relay secret. Returns the relay-chosen
 * feed_id (the URL token), the expiry, and creation time.
 */
export async function mintFeed(
  relay: RelayConfig,
  ttlSeconds: number,
  memberCard: Member,
  signal?: AbortSignal,
): Promise<MintResult> {
  const path = "/feeds";
  const body = JSON.stringify({ ttl_seconds: Math.floor(ttlSeconds), member_card: memberCard });
  const res = await fetch(stripTrailingSlash(relay.url) + path, {
    method: "POST",
    headers: {
      Authorization: relayAuthHeader(relay.secret, "POST", path),
      "Content-Type": "application/json",
    },
    body,
    signal,
  });
  if (res.status === 201) return (await res.json()) as MintResult;
  throw new RelayError(res.status, (await safeReason(res)) || "mint-failed");
}

/** Feed-key-signed client for a single relay host. */
export class FeedClient {
  constructor(private readonly host: string) {}

  private url(path: string): string {
    return stripTrailingSlash(this.host) + path;
  }

  private async send(
    feedKey: Uint8Array,
    method: string,
    path: string,
    opts: { body?: BodyInit; contentType?: string; signal?: AbortSignal } = {},
  ): Promise<Response> {
    const headers: Record<string, string> = { Authorization: feedAuthHeader(feedKey, method, path) };
    if (opts.body !== undefined) headers["Content-Type"] = opts.contentType ?? "application/octet-stream";
    return fetch(this.url(path), { method, headers, body: opts.body, signal: opts.signal });
  }

  async getMeta(feedId: string, feedKey: Uint8Array, signal?: AbortSignal): Promise<{ expires_at: number }> {
    const res = await this.send(feedKey, "GET", `/feeds/${feedId}/meta`, { signal });
    if (res.status === 200) return (await res.json()) as { expires_at: number };
    throw new RelayError(res.status, (await safeReason(res)) || "meta-failed");
  }

  async putMember(
    feedId: string,
    feedKey: Uint8Array,
    memberId: string,
    card: Member,
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await this.send(feedKey, "PUT", `/feeds/${feedId}/members/${memberId}`, {
      body: JSON.stringify(card),
      contentType: "application/json",
      signal,
    });
    if (res.status === 204) return;
    throw new RelayError(res.status, (await safeReason(res)) || "put-member-failed");
  }

  /** Leave a feed. Best-effort: never throws (mirrors _relay_delete_member). */
  async deleteMember(feedId: string, feedKey: Uint8Array, memberId: string, signal?: AbortSignal): Promise<void> {
    try {
      await this.send(feedKey, "DELETE", `/feeds/${feedId}/members/${memberId}`, { signal });
    } catch {
      // leave is opportunistic
    }
  }

  async listMembers(
    feedId: string,
    feedKey: Uint8Array,
    opts: { sinceTs?: number; waitMs?: number; signal?: AbortSignal } = {},
  ): Promise<Member[]> {
    const path = `/feeds/${feedId}/members${qs({ wait: opts.waitMs, since: opts.sinceTs })}`;
    const res = await this.send(feedKey, "GET", path, { signal: opts.signal });
    if (res.status === 200) {
      const parsed = (await res.json()) as { members?: Member[] };
      return parsed.members ?? [];
    }
    throw new RelayError(res.status, (await safeReason(res)) || "list-members-failed");
  }

  async extend(feedId: string, feedKey: Uint8Array, newTtlSeconds: number, signal?: AbortSignal): Promise<{ expires_at: number }> {
    const res = await this.send(feedKey, "POST", `/feeds/${feedId}/extend`, {
      body: JSON.stringify({ new_ttl_seconds: Math.floor(newTtlSeconds) }),
      contentType: "application/json",
      signal,
    });
    if (res.status === 200) return (await res.json()) as { expires_at: number };
    throw new RelayError(res.status, (await safeReason(res)) || "extend-failed");
  }

  async close(feedId: string, feedKey: Uint8Array, signal?: AbortSignal): Promise<void> {
    const res = await this.send(feedKey, "DELETE", `/feeds/${feedId}`, { signal });
    if (res.status === 204 || res.status === 404) return;
    throw new RelayError(res.status, (await safeReason(res)) || "close-failed");
  }

  async putSlot(
    feedId: string,
    feedKey: Uint8Array,
    slotId: string,
    body: Uint8Array,
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await this.send(feedKey, "PUT", `/feeds/${feedId}/slots/${slotId}`, {
      body: body as BodyInit,
      signal,
    });
    if (res.status === 204) return;
    throw new RelayError(res.status, (await safeReason(res)) || "put-slot-failed");
  }

  /** Fetch a slot (no delete-on-read). Returns null on 404. Long-polls up to waitMs. */
  async getSlot(
    feedId: string,
    feedKey: Uint8Array,
    slotId: string,
    opts: { waitMs?: number; signal?: AbortSignal } = {},
  ): Promise<ArrayBuffer | null> {
    const path = `/feeds/${feedId}/slots/${slotId}${qs({ wait: opts.waitMs })}`;
    const res = await this.send(feedKey, "GET", path, { signal: opts.signal });
    if (res.status === 200) return await res.arrayBuffer();
    if (res.status === 404) return null;
    throw new RelayError(res.status, (await safeReason(res)) || "get-slot-failed");
  }

  async listSlots(
    feedId: string,
    feedKey: Uint8Array,
    opts: { sinceTs?: number; waitMs?: number; signal?: AbortSignal } = {},
  ): Promise<SlotMeta[]> {
    const path = `/feeds/${feedId}/slots${qs({ wait: opts.waitMs, since: opts.sinceTs })}`;
    const res = await this.send(feedKey, "GET", path, { signal: opts.signal });
    if (res.status === 200) {
      const parsed = (await res.json()) as { slots?: SlotMeta[] };
      return parsed.slots ?? [];
    }
    throw new RelayError(res.status, (await safeReason(res)) || "list-slots-failed");
  }

  /**
   * Push stream of new-slot notifications over SSE (the /stream endpoint backed
   * by a Durable Object). Yields {slot_id, created_at} as posts arrive — the
   * caller still GETs each slot body. Reconnects itself with a freshly signed
   * URL (EventSource can't set an Authorization header, so the feed-key MAC
   * rides the `?auth=` query; reopening keeps its timestamp inside the relay's
   * skew window). `getSince` is read at each (re)connect so replay resumes from
   * the caller's current cursor. Stops when `signal` aborts. Throws
   * StreamUnsupportedError if /stream never opens (relay predates it).
   */
  async *streamSlotEvents(
    feedId: string,
    feedKey: Uint8Array,
    opts: { getSince: () => number; signal: AbortSignal },
  ): AsyncGenerator<SlotMeta> {
    const { getSince, signal } = opts;
    const barePath = `/feeds/${feedId}/stream`;
    let failsBeforeOpen = 0;

    while (!signal.aborted) {
      const ts = Math.floor(Date.now() / 1000);
      const mac = feedRequestMac(feedKey, "GET", barePath, ts);
      const url =
        this.url(barePath) + `?since=${getSince()}&auth=${ts}:${mac}`;
      const es = new EventSource(url);

      const queue: SlotMeta[] = [];
      let opened = false;
      let dead = false;
      let wake: (() => void) | null = null;
      const ping = () => {
        if (wake) {
          const w = wake;
          wake = null;
          w();
        }
      };
      es.onopen = () => {
        opened = true;
        failsBeforeOpen = 0;
      };
      es.onmessage = (ev: MessageEvent) => {
        try {
          const d = JSON.parse(ev.data) as { slot_id?: unknown; created_at?: unknown };
          if (typeof d.slot_id === "string") {
            queue.push({
              slot_id: d.slot_id,
              created_at: typeof d.created_at === "number" ? d.created_at : 0,
            });
          }
        } catch {
          // skip a malformed frame
        }
        ping();
      };
      es.onerror = () => {
        dead = true;
        ping();
      };
      const onAbort = () => {
        dead = true;
        ping();
      };
      signal.addEventListener("abort", onAbort, { once: true });

      try {
        // Invariant: every state change (new queue item, error, abort) calls
        // ping(), and the loop re-checks queue.length / dead / aborted at the
        // top before awaiting again. So a ping() that fires while `wake` is null
        // (consumer mid-yield) is benign — the next iteration observes the change.
        while (!signal.aborted && !dead) {
          if (queue.length === 0) {
            await new Promise<void>((r) => (wake = r));
            continue;
          }
          yield queue.shift()!;
        }
      } finally {
        es.close();
        signal.removeEventListener("abort", onAbort);
      }

      if (signal.aborted) break;
      // Errored. If it never opened, the endpoint is likely absent — give up
      // after a few tries so the caller can fall back to long-poll.
      if (!opened) {
        failsBeforeOpen++;
        if (failsBeforeOpen >= STREAM_UNSUPPORTED_RETRIES) {
          throw new StreamUnsupportedError();
        }
      }
      await sleep(STREAM_RECONNECT_MS, signal);
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
