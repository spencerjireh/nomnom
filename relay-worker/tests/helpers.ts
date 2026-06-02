// Test helpers: signed-request builders for the relay's two auth schemes.

const HMAC_PREFIX = "NMNM-HMAC-SHA256";
const FEED_KEY_PREFIX = "NMNM-FEEDKEY-SHA256";
const HKDF_SALT = new TextEncoder().encode("nomnom-feed-v1");

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
  const mac = await hmacSha256Hex(
    new TextEncoder().encode(TEST_SECRET),
    msg,
    "raw",
  );
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
  const feedKey = await deriveFeedKeyBytes(feedId);
  const mac = await hmacSha256Hex(feedKey, msg, "raw-bytes");
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

export async function deriveFeedKeyBytes(feedId: string): Promise<Uint8Array> {
  const ikm = urlsafeBase64Decode(feedId);
  const info = new TextEncoder().encode(feedId);
  const baseKey = await crypto.subtle.importKey(
    "raw",
    ikm,
    "HKDF",
    false,
    ["deriveBits"],
  );
  const bits = await crypto.subtle.deriveBits(
    { name: "HKDF", hash: "SHA-256", salt: HKDF_SALT, info },
    baseKey,
    256,
  );
  return new Uint8Array(bits);
}

async function hmacSha256Hex(
  key: Uint8Array,
  msg: string,
  _label: string,
): Promise<string> {
  const cryptoKey = await crypto.subtle.importKey(
    "raw",
    key,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    cryptoKey,
    new TextEncoder().encode(msg),
  );
  return bytesToHex(new Uint8Array(sig));
}

function bytesToHex(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i++) {
    out += bytes[i].toString(16).padStart(2, "0");
  }
  return out;
}

function urlsafeBase64Decode(s: string): Uint8Array {
  let padded = s.replace(/-/g, "+").replace(/_/g, "/");
  while (padded.length % 4 !== 0) padded += "=";
  const bin = atob(padded);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

export function randomMemberId(): string {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return bytesToHex(bytes);
}

export function randomBase64(byteLen: number): string {
  const bytes = new Uint8Array(byteLen);
  crypto.getRandomValues(bytes);
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
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
