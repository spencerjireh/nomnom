// HTTP + WebSocket client for the relay Worker's /feeds/* endpoints. Status
// semantics mirror nomnom.py's _relay_mint_feed / _relay_* feed helpers.
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
import { WS_PING_INTERVAL_MS, WS_RECONNECT_MAX_MS, WS_RECONNECT_MIN_MS } from "../config";
import { sleep } from "../util/sleep";
import type { Member, RelayConfig } from "../types";
import { RelayError } from "./errors";

export interface MintResult {
  feed_id: string;
  created_at: number;
}

export interface FeedMeta {
  created_at: number;
  last_used_at: number;
}

/** One post in the relay's index. `seq` is the feed's monotonic cursor. */
export interface SlotMeta {
  seq: number;
  slot_id: string;
  created_at: number;
}

/** A frame pushed over the /ws socket. */
export type FeedFrame =
  | { type: "post"; seq: number; slot_id: string; created_at: number }
  | { type: "member"; action: "join" | "leave"; member: Member }
  | { type: "deleted"; slot_id: string };

/**
 * Parse a raw socket message into a FeedFrame, or null for anything that is
 * not one: the "pong" keepalive reply, non-JSON, or an unknown/malformed type.
 */
export function parseFrame(data: unknown): FeedFrame | null {
  if (typeof data !== "string") return null;
  let obj: unknown;
  try {
    obj = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof obj !== "object" || obj === null) return null;
  const f = obj as Record<string, unknown>;
  switch (f.type) {
    case "post":
      if (
        typeof f.seq === "number" &&
        typeof f.slot_id === "string" &&
        typeof f.created_at === "number"
      ) {
        return { type: "post", seq: f.seq, slot_id: f.slot_id, created_at: f.created_at };
      }
      return null;
    case "member": {
      const m = f.member as Record<string, unknown> | undefined;
      if (
        (f.action === "join" || f.action === "leave") &&
        m &&
        typeof m.member_id === "string" &&
        typeof m.identity_pubkey === "string" &&
        typeof m.name === "string"
      ) {
        const member: Member = {
          member_id: m.member_id,
          identity_pubkey: m.identity_pubkey,
          name: m.name,
        };
        if (typeof m.joined_at === "number") member.joined_at = m.joined_at;
        return { type: "member", action: f.action, member };
      }
      return null;
    }
    case "deleted":
      if (typeof f.slot_id === "string") return { type: "deleted", slot_id: f.slot_id };
      return null;
    default:
      return null;
  }
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
 * feed_id (the URL token) and creation time.
 */
export async function mintFeed(
  relay: RelayConfig,
  memberCard: Member,
  signal?: AbortSignal,
): Promise<MintResult> {
  const path = "/feeds";
  const body = JSON.stringify({ member_card: memberCard });
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

  private wsUrl(path: string): string {
    return this.url(path).replace(/^http(s?):/, "ws$1:");
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

  async getMeta(feedId: string, feedKey: Uint8Array, signal?: AbortSignal): Promise<FeedMeta> {
    const res = await this.send(feedKey, "GET", `/feeds/${feedId}/meta`, { signal });
    if (res.status === 200) return (await res.json()) as FeedMeta;
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
    opts: { signal?: AbortSignal } = {},
  ): Promise<Member[]> {
    const res = await this.send(feedKey, "GET", `/feeds/${feedId}/members`, { signal: opts.signal });
    if (res.status === 200) {
      const parsed = (await res.json()) as { members?: Member[] };
      return parsed.members ?? [];
    }
    throw new RelayError(res.status, (await safeReason(res)) || "list-members-failed");
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

  /** Fetch a post body. Returns null on 404 (never posted, deleted, or aged out). */
  async getSlot(
    feedId: string,
    feedKey: Uint8Array,
    slotId: string,
    opts: { signal?: AbortSignal } = {},
  ): Promise<ArrayBuffer | null> {
    const res = await this.send(feedKey, "GET", `/feeds/${feedId}/slots/${slotId}`, {
      signal: opts.signal,
    });
    if (res.status === 200) return await res.arrayBuffer();
    if (res.status === 404) return null;
    throw new RelayError(res.status, (await safeReason(res)) || "get-slot-failed");
  }

  /**
   * Hard-delete a post for every device. 204 and 404 both resolve: the goal is
   * "gone", and a post that is already gone satisfies it.
   */
  async deleteSlot(feedId: string, feedKey: Uint8Array, slotId: string, signal?: AbortSignal): Promise<void> {
    const res = await this.send(feedKey, "DELETE", `/feeds/${feedId}/slots/${slotId}`, { signal });
    if (res.status === 204 || res.status === 404) return;
    throw new RelayError(res.status, (await safeReason(res)) || "delete-slot-failed");
  }

  /** List posts with seq > `since`, ascending. `waitMs` long-polls when empty. */
  async listSlots(
    feedId: string,
    feedKey: Uint8Array,
    opts: { since?: number; waitMs?: number; signal?: AbortSignal } = {},
  ): Promise<SlotMeta[]> {
    const path = `/feeds/${feedId}/slots${qs({ wait: opts.waitMs, since: opts.since })}`;
    const res = await this.send(feedKey, "GET", path, { signal: opts.signal });
    if (res.status === 200) {
      const parsed = (await res.json()) as { slots?: SlotMeta[] };
      return parsed.slots ?? [];
    }
    throw new RelayError(res.status, (await safeReason(res)) || "list-slots-failed");
  }

  /**
   * Live feed events over the /ws WebSocket. Yields `post` (replay of seq >
   * getSince() on connect, then live), `member` (join/leave), and `deleted`
   * frames. The caller still GETs each post body.
   *
   * A browser WebSocket can't set an Authorization header, so the feed-key MAC
   * rides the `?auth=` query. On any drop the generator sleeps with doubling
   * backoff and reconnects with a freshly signed URL and the caller's current
   * cursor (`getSince` is read at each connect). Stops only when `signal`
   * aborts; there is no give-up path.
   */
  async *watch(
    feedId: string,
    feedKey: Uint8Array,
    opts: { getSince: () => number; signal: AbortSignal },
  ): AsyncGenerator<FeedFrame> {
    const { getSince, signal } = opts;
    const barePath = `/feeds/${feedId}/ws`;
    let backoff = WS_RECONNECT_MIN_MS;

    while (!signal.aborted) {
      const ts = Math.floor(Date.now() / 1000);
      const mac = feedRequestMac(feedKey, "GET", barePath, ts);
      const ws = new WebSocket(`${this.wsUrl(barePath)}?since=${getSince()}&auth=${ts}:${mac}`);

      const queue: FeedFrame[] = [];
      let dead = false;
      let wake: (() => void) | null = null;
      let pingTimer: ReturnType<typeof setInterval> | null = null;
      const ping = () => {
        if (wake) {
          const w = wake;
          wake = null;
          w();
        }
      };
      ws.onopen = () => {
        backoff = WS_RECONNECT_MIN_MS;
        pingTimer = setInterval(() => {
          if (ws.readyState === ws.OPEN) ws.send("ping");
        }, WS_PING_INTERVAL_MS);
      };
      ws.onmessage = (ev: MessageEvent) => {
        const frame = parseFrame(ev.data);
        if (frame) {
          queue.push(frame);
          ping();
        }
      };
      ws.onerror = () => {
        dead = true;
        ping();
      };
      ws.onclose = () => {
        dead = true;
        ping();
      };
      const onAbort = () => {
        dead = true;
        ping();
      };
      signal.addEventListener("abort", onAbort, { once: true });

      try {
        // Invariant: every state change (new queue item, close, abort) calls
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
        if (pingTimer) clearInterval(pingTimer);
        signal.removeEventListener("abort", onAbort);
        // Detach before closing so our own close() can't re-enter the loop.
        ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
        try {
          ws.close();
        } catch {
          // already closed
        }
      }

      if (signal.aborted) break;
      await sleep(backoff, signal);
      backoff = Math.min(backoff * 2, WS_RECONNECT_MAX_MS);
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
