// Test helpers: signed-request builders for the relay's two auth schemes.
// All crypto + encoding primitives come from `../src/` so the tests can't drift
// out of sync with the production code they're checking.

import {
  bytesToHex,
  hmacSha256Hex,
  urlsafeBase64Encode,
} from "../src/crypto-util";
import { deriveFeedKey } from "../src/feed-auth";
import type { Feed } from "../src/feed";
import { env, SELF } from "cloudflare:test";

declare module "cloudflare:test" {
  interface ProvidedEnv {
    BUCKET: R2Bucket;
    FEED: DurableObjectNamespace<Feed>;
  }
}

const HMAC_PREFIX = "NMNM-HMAC-SHA256";
const FEED_KEY_PREFIX = "NMNM-FEEDKEY-SHA256";

export const TEST_SECRET = "test-secret-do-not-use-in-prod";
export const BASE = "https://relay.test";

function pathOnly(p: string): string {
  const q = p.indexOf("?");
  return q < 0 ? p : p.slice(0, q);
}

export async function signedHmacRequest(
  method: string,
  path: string,
  opts: {
    body?: BodyInit;
    contentLength?: number;
    tsOverride?: number;
    secret?: string;
  } = {},
): Promise<Request> {
  const ts = opts.tsOverride ?? Math.floor(Date.now() / 1000);
  const msg = `${method}\n${pathOnly(path)}\n${ts}`;
  const mac = await hmacSha256Hex(opts.secret ?? TEST_SECRET, msg);
  const headers: Record<string, string> = {
    Authorization: `${HMAC_PREFIX} ${ts}:${mac}`,
  };
  if (opts.contentLength !== undefined) {
    headers["Content-Length"] = String(opts.contentLength);
  }
  return new Request(`${BASE}${path}`, {
    method,
    headers,
    body: opts.body,
  });
}

export async function signedFeedRequest(
  method: string,
  path: string,
  feedId: string,
  opts: { body?: BodyInit; contentLength?: number } = {},
): Promise<Request> {
  const ts = Math.floor(Date.now() / 1000);
  const msg = `${method}\n${pathOnly(path)}\n${ts}`;
  const feedKey = await deriveFeedKey(feedId);
  const mac = await hmacSha256Hex(feedKey, msg);
  const headers: Record<string, string> = {
    Authorization: `${FEED_KEY_PREFIX} ${ts}:${mac}`,
  };
  if (opts.contentLength !== undefined) {
    headers["Content-Length"] = String(opts.contentLength);
  }
  return new Request(`${BASE}${path}`, {
    method,
    headers,
    body: opts.body,
  });
}

// Build a /ws URL carrying the feed-key MAC in the `?auth=` query param (the
// shape a browser WebSocket must use, since it can't set headers). `tsOverride`
// lets a test forge an out-of-window timestamp to exercise the clock-skew gate.
export async function feedWsUrl(
  feedId: string,
  sinceSeq = 0,
  opts: { tsOverride?: number; macOverride?: string } = {},
): Promise<string> {
  const path = `/feeds/${feedId}/ws`;
  const ts = opts.tsOverride ?? Math.floor(Date.now() / 1000);
  const feedKey = await deriveFeedKey(feedId);
  const mac =
    opts.macOverride ?? (await hmacSha256Hex(feedKey, `GET\n${path}\n${ts}`));
  return `${BASE}${path}?since=${sinceSeq}&auth=${ts}:${mac}`;
}

export interface WsFrame {
  type: string;
  seq?: number;
  slot_id?: string;
  created_at?: number;
  action?: string;
  member?: { member_id: string; name: string };
}

export interface OpenWs {
  status: number;
  ws: WebSocket | null;
  frames: WsFrame[];
  // Resolve once at least `n` frames have arrived, or after `budgetMs`.
  next(n: number, budgetMs?: number): Promise<WsFrame[]>;
  close(): void;
}

// Open a /ws socket through the Worker. Tests must call `close()` in `finally`
// so isolated-storage teardown finds the DO idle.
export async function openFeedWs(url: string): Promise<OpenWs> {
  const res = await SELF.fetch(url, { headers: { Upgrade: "websocket" } });
  const frames: WsFrame[] = [];
  const waiters: { n: number; resolve: () => void }[] = [];
  const ws = res.webSocket ?? null;
  if (ws) {
    ws.accept();
    ws.addEventListener("message", (ev) => {
      if (typeof ev.data !== "string") return;
      try {
        frames.push(JSON.parse(ev.data) as WsFrame);
      } catch {
        return;
      }
      for (const w of [...waiters]) {
        if (frames.length >= w.n) {
          waiters.splice(waiters.indexOf(w), 1);
          w.resolve();
        }
      }
    });
  }
  return {
    status: res.status,
    ws,
    frames,
    next(n, budgetMs = 3000) {
      if (frames.length >= n) return Promise.resolve(frames.slice());
      return new Promise<WsFrame[]>((resolve) => {
        const w = { n, resolve: () => { clearTimeout(t); resolve(frames.slice()); } };
        const t = setTimeout(() => {
          const i = waiters.indexOf(w);
          if (i >= 0) waiters.splice(i, 1);
          resolve(frames.slice());
        }, budgetMs);
        waiters.push(w);
      });
    },
    close() {
      try {
        ws?.close(1000, "test-done");
      } catch {
        // already closed
      }
    },
  };
}

export async function putSlot(
  feedId: string,
  slotId: string,
  body = "p",
): Promise<Response> {
  const bytes = new TextEncoder().encode(body);
  return SELF.fetch(
    await signedFeedRequest("PUT", `/feeds/${feedId}/slots/${slotId}`, feedId, {
      body: bytes,
      contentLength: bytes.byteLength,
    }),
  );
}

export function feedStub(feedId: string): DurableObjectStub<Feed> {
  return env.FEED.get(env.FEED.idFromName(feedId));
}

export function randomMemberId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return bytesToHex(bytes);
}

export function randomBase64(byteLen: number): string {
  const bytes = new Uint8Array(byteLen);
  crypto.getRandomValues(bytes);
  return urlsafeBase64Encode(bytes);
}

export interface MintedFeed {
  feed_id: string;
  created_at: number;
  member_id: string;
}

export async function mintFeed(
  fetcher: Fetcher,
  opts: { name?: string; identityPubkey?: string } = {},
): Promise<MintedFeed> {
  const memberId = randomMemberId();
  const body = JSON.stringify({
    member_card: {
      member_id: memberId,
      identity_pubkey: opts.identityPubkey ?? randomBase64(32),
      name: opts.name ?? "test-device",
    },
  });
  const req = await signedHmacRequest("POST", "/feeds", { body });
  const res = await fetcher.fetch(req);
  if (res.status !== 201) {
    throw new Error(`mint failed: ${res.status} ${await res.text()}`);
  }
  const parsed = (await res.json()) as { feed_id: string; created_at: number };
  return { ...parsed, member_id: memberId };
}
