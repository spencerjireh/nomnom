// Test helpers: signed-request builders for the relay's two auth schemes.
// All crypto + encoding primitives come from `../src/` so the tests can't drift
// out of sync with the production code they're checking.

import {
  bytesToHex,
  hmacSha256Hex,
  urlsafeBase64Encode,
} from "../src/crypto-util";
import { deriveFeedKey } from "../src/feed-auth";

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
  opts: { body?: BodyInit; contentLength?: number } = {},
): Promise<Request> {
  const ts = Math.floor(Date.now() / 1000);
  const msg = `${method}\n${pathOnly(path)}\n${ts}`;
  const mac = await hmacSha256Hex(TEST_SECRET, msg);
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

// Build a /stream URL carrying the feed-key MAC in the `?auth=` query param
// (the shape an EventSource must use, since it can't set headers). `tsOverride`
// lets a test forge an out-of-window timestamp to exercise the clock-skew gate.
export async function feedStreamUrl(
  feedId: string,
  sinceTs = 0,
  opts: { tsOverride?: number; macOverride?: string } = {},
): Promise<string> {
  const path = `/feeds/${feedId}/stream`;
  const ts = opts.tsOverride ?? Math.floor(Date.now() / 1000);
  const feedKey = await deriveFeedKey(feedId);
  const mac =
    opts.macOverride ?? (await hmacSha256Hex(feedKey, `GET\n${path}\n${ts}`));
  return `${BASE}${path}?since=${sinceTs}&auth=${ts}:${mac}`;
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
  expires_at: number;
  created_at: number;
  member_id: string;
}

export async function mintFeed(
  fetcher: Fetcher,
  opts: {
    name?: string;
    identityPubkey?: string;
    ttlSeconds?: number;
  } = {},
): Promise<MintedFeed> {
  const memberId = randomMemberId();
  const body = JSON.stringify({
    ttl_seconds: opts.ttlSeconds ?? 3600,
    member_card: {
      member_id: memberId,
      identity_pubkey: opts.identityPubkey ?? randomBase64(32),
      name: opts.name ?? "test-device",
    },
  });
  const req = await signedHmacRequest("POST", "/feeds", { body });
  const res = await fetcher.fetch(req);
  if (res.status !== 201) {
    throw new Error(
      `mint failed: ${res.status} ${await res.text()}`,
    );
  }
  const parsed = (await res.json()) as {
    feed_id: string;
    expires_at: number;
    created_at: number;
  };
  return { ...parsed, member_id: memberId };
}
