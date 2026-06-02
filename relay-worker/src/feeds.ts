// Feed lifecycle and storage on R2.
//
// Storage layout (prefix: feeds/<feed_id>/):
//   feeds/<id>/meta              — JSON {created_at, expires_at}
//   feeds/<id>/members/<mid>     — JSON member card
//   feeds/<id>/slots/<slot_id>   — raw ciphertext (encrypted file)
//
// All objects carry `customMetadata.expires_at` matching the feed's expiry.
// Cleanup is the R2 bucket lifecycle's job (1-day TTL).
//
// Auth shape:
//   POST /feeds                  — relay HMAC (gates who can mint feeds)
//   /feeds/:id/*                 — feed-key signature (URL token IS the credential)

import { pollSlot } from "./poll";

const FEED_ID_RE = /^[A-Za-z0-9_-]{8,32}$/;
const MEMBER_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
const SLOT_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

const DEFAULT_TTL_SEC = 86_400; // 1 day
const MAX_TTL_SEC = 90 * 86_400; // 90 days
const MIN_TTL_SEC = 60; // 1 minute (sanity floor)
const MAX_MEMBER_CARD_BYTES = 4096;
const MAX_BODY_BYTES = 256 * 1024 * 1024; // 256 MB
const MAX_MEMBER_COUNT = 64; // bound roster size per feed

const MEMBER_POLL_INTERVAL_MS = 1000;
const SLOT_LIST_POLL_INTERVAL_MS = 1000;
const MAX_BUDGET_MS = 30_000;

export function validateFeedId(id: string): boolean {
  return FEED_ID_RE.test(id);
}

export function validateMemberId(id: string): boolean {
  return MEMBER_ID_RE.test(id);
}

export function validateSlotId(id: string): boolean {
  return SLOT_ID_RE.test(id);
}

// ---------- POST /feeds ----------

interface MintBody {
  ttl_seconds?: number;
  member_card: {
    member_id: string;
    identity_pubkey: string;
    name: string;
  };
}

export async function mintFeed(
  bucket: R2Bucket,
  req: Request,
): Promise<Response> {
  let body: MintBody;
  try {
    const text = await req.text();
    body = JSON.parse(text);
  } catch {
    return new Response("bad-json", { status: 400 });
  }

  const ttl = clampTtl(body.ttl_seconds);
  const card = body.member_card;
  if (
    !card ||
    typeof card.member_id !== "string" ||
    !validateMemberId(card.member_id) ||
    typeof card.identity_pubkey !== "string" ||
    typeof card.name !== "string"
  ) {
    return new Response("bad-member-card", { status: 400 });
  }
  const cardJson = JSON.stringify(card);
  if (cardJson.length > MAX_MEMBER_CARD_BYTES) {
    return new Response("member-card-too-large", { status: 413 });
  }

  const feedId = generateFeedId();
  const now = Math.floor(Date.now() / 1000);
  const expiresAt = now + ttl;
  const expiresAtStr = String(expiresAt);

  const meta = JSON.stringify({ created_at: now, expires_at: expiresAt });
  await bucket.put(`feeds/${feedId}/meta`, meta, {
    customMetadata: { expires_at: expiresAtStr },
    httpMetadata: { contentType: "application/json" },
  });
  await bucket.put(`feeds/${feedId}/members/${card.member_id}`, cardJson, {
    customMetadata: {
      expires_at: expiresAtStr,
      created_at: String(now),
    },
    httpMetadata: { contentType: "application/json" },
  });

  return jsonResponse(
    {
      feed_id: feedId,
      expires_at: expiresAt,
      created_at: now,
    },
    201,
  );
}

// ---------- GET /feeds/:id/meta ----------

export async function getFeedMeta(
  bucket: R2Bucket,
  feedId: string,
): Promise<Response> {
  const obj = await bucket.get(`feeds/${feedId}/meta`);
  if (obj === null) {
    return new Response("feed-not-found", { status: 404 });
  }
  if (isExpired(obj.customMetadata)) {
    await purgeFeed(bucket, feedId);
    return new Response("feed-expired", { status: 410 });
  }
  return new Response(obj.body, {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

// ---------- POST /feeds/:id/extend ----------

export async function extendFeed(
  bucket: R2Bucket,
  feedId: string,
  req: Request,
): Promise<Response> {
  let body: { new_ttl_seconds?: number };
  try {
    body = JSON.parse(await req.text());
  } catch {
    return new Response("bad-json", { status: 400 });
  }
  const ttl = clampTtl(body.new_ttl_seconds);

  const metaObj = await bucket.get(`feeds/${feedId}/meta`);
  if (metaObj === null) {
    return new Response("feed-not-found", { status: 404 });
  }
  if (isExpired(metaObj.customMetadata)) {
    await purgeFeed(bucket, feedId);
    return new Response("feed-expired", { status: 410 });
  }
  const meta = JSON.parse(await metaObj.text()) as {
    created_at: number;
  };
  const now = Math.floor(Date.now() / 1000);
  const newExpiresAt = now + ttl;
  const updated = JSON.stringify({
    created_at: meta.created_at,
    expires_at: newExpiresAt,
  });
  await bucket.put(`feeds/${feedId}/meta`, updated, {
    customMetadata: { expires_at: String(newExpiresAt) },
    httpMetadata: { contentType: "application/json" },
  });
  // Note: per-object expires_at on members/slots stays at old value. R2 lifecycle
  // cleans by age, not metadata; we honour the new feed TTL via meta-level checks.
  return jsonResponse({ expires_at: newExpiresAt }, 200);
}

// ---------- DELETE /feeds/:id ----------

export async function closeFeed(
  bucket: R2Bucket,
  feedId: string,
): Promise<Response> {
  await purgeFeed(bucket, feedId);
  return new Response(null, { status: 204 });
}

// ---------- PUT /feeds/:id/members/:member_id ----------

export async function putMember(
  bucket: R2Bucket,
  feedId: string,
  memberId: string,
  req: Request,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId);
  if (live.status !== 200) return live;

  const text = await req.text();
  if (text.length > MAX_MEMBER_CARD_BYTES) {
    return new Response("member-card-too-large", { status: 413 });
  }
  let card: { member_id?: string; identity_pubkey?: string; name?: string };
  try {
    card = JSON.parse(text);
  } catch {
    return new Response("bad-json", { status: 400 });
  }
  if (
    typeof card.member_id !== "string" ||
    card.member_id !== memberId ||
    typeof card.identity_pubkey !== "string" ||
    typeof card.name !== "string"
  ) {
    return new Response("bad-member-card", { status: 400 });
  }

  // Bound roster growth.
  const existing = await bucket.list({
    prefix: `feeds/${feedId}/members/`,
    limit: MAX_MEMBER_COUNT + 1,
  });
  const isUpdate = existing.objects.some(
    (o) => o.key === `feeds/${feedId}/members/${memberId}`,
  );
  if (!isUpdate && existing.objects.length >= MAX_MEMBER_COUNT) {
    return new Response("feed-full", { status: 409 });
  }

  const expiresAt = await readExpiresAt(bucket, feedId);
  const now = Math.floor(Date.now() / 1000);
  await bucket.put(`feeds/${feedId}/members/${memberId}`, text, {
    customMetadata: {
      expires_at: String(expiresAt ?? now + DEFAULT_TTL_SEC),
      created_at: String(now),
    },
    httpMetadata: { contentType: "application/json" },
  });
  return new Response(null, { status: 204 });
}

// ---------- DELETE /feeds/:id/members/:member_id ----------

export async function deleteMember(
  bucket: R2Bucket,
  feedId: string,
  memberId: string,
): Promise<Response> {
  await bucket.delete(`feeds/${feedId}/members/${memberId}`);
  return new Response(null, { status: 204 });
}

// ---------- GET /feeds/:id/members ----------

export async function listMembers(
  bucket: R2Bucket,
  feedId: string,
  waitMs: number,
  sinceTs: number,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId);
  if (live.status !== 200) return live;

  const deadline = Date.now() + Math.min(Math.max(waitMs, 0), MAX_BUDGET_MS);
  while (true) {
    const result = await readMembersSince(bucket, feedId, sinceTs);
    if (result.fresh.length > 0 || waitMs <= 0) {
      return jsonResponse({ members: result.all, fresh: result.fresh }, 200);
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      return jsonResponse({ members: result.all, fresh: [] }, 200);
    }
    await sleep(Math.min(MEMBER_POLL_INTERVAL_MS, remaining));
  }
}

interface MemberCard {
  member_id: string;
  identity_pubkey: string;
  name: string;
  joined_at: number;
}

async function readMembersSince(
  bucket: R2Bucket,
  feedId: string,
  sinceTs: number,
): Promise<{ all: MemberCard[]; fresh: MemberCard[] }> {
  const list = await bucket.list({
    prefix: `feeds/${feedId}/members/`,
    limit: MAX_MEMBER_COUNT + 1,
  });
  const cards: MemberCard[] = [];
  const fresh: MemberCard[] = [];
  for (const obj of list.objects) {
    const head = await bucket.head(obj.key);
    const joinedAt = parseTs(head?.customMetadata?.created_at) ?? 0;
    const body = await bucket.get(obj.key);
    if (body === null) continue;
    let parsed: { member_id: string; identity_pubkey: string; name: string };
    try {
      parsed = JSON.parse(await body.text());
    } catch {
      continue;
    }
    const card: MemberCard = {
      member_id: parsed.member_id,
      identity_pubkey: parsed.identity_pubkey,
      name: parsed.name,
      joined_at: joinedAt,
    };
    cards.push(card);
    if (joinedAt > sinceTs) fresh.push(card);
  }
  return { all: cards, fresh };
}

// ---------- PUT /feeds/:id/slots/:slot_id ----------

export async function putFeedSlot(
  bucket: R2Bucket,
  feedId: string,
  slotId: string,
  req: Request,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId);
  if (live.status !== 200) return live;

  const lenHdr = req.headers.get("Content-Length");
  if (lenHdr !== null) {
    const len = Number.parseInt(lenHdr, 10);
    if (Number.isFinite(len) && len > MAX_BODY_BYTES) {
      return new Response("payload-too-large", { status: 413 });
    }
  }
  const key = `feeds/${feedId}/slots/${slotId}`;
  const existing = await bucket.head(key);
  if (existing !== null) {
    return new Response("slot-occupied", { status: 409 });
  }
  if (req.body === null) {
    return new Response("empty-body", { status: 400 });
  }
  const expiresAt =
    (await readExpiresAt(bucket, feedId)) ??
    Math.floor(Date.now() / 1000) + DEFAULT_TTL_SEC;
  const now = Math.floor(Date.now() / 1000);
  await bucket.put(key, req.body, {
    customMetadata: {
      expires_at: String(expiresAt),
      created_at: String(now),
    },
  });
  return new Response(null, { status: 204 });
}

// ---------- GET /feeds/:id/slots/:slot_id ----------
// Multi-party broadcast: NO delete-on-read. Slot lives until feed TTL.

export async function getFeedSlot(
  bucket: R2Bucket,
  feedId: string,
  slotId: string,
  waitMs: number,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId);
  if (live.status !== 200) return live;

  const key = `feeds/${feedId}/slots/${slotId}`;
  const obj = await pollSlot(bucket, key, waitMs);
  if (obj === null) {
    return new Response("not-found", { status: 404 });
  }
  if (isExpired(obj.customMetadata)) {
    await bucket.delete(key);
    return new Response("expired", { status: 410 });
  }
  return new Response(obj.body, {
    status: 200,
    headers: { "Content-Type": "application/octet-stream" },
  });
}

// ---------- GET /feeds/:id/slots?since=<ts>&wait=<ms> ----------
// Long-poll list of new slot ids since a timestamp. Receivers use this to
// discover new posts.

export async function listFeedSlots(
  bucket: R2Bucket,
  feedId: string,
  waitMs: number,
  sinceTs: number,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId);
  if (live.status !== 200) return live;

  const deadline = Date.now() + Math.min(Math.max(waitMs, 0), MAX_BUDGET_MS);
  while (true) {
    const fresh = await readSlotsSince(bucket, feedId, sinceTs);
    if (fresh.length > 0 || waitMs <= 0) {
      return jsonResponse({ slots: fresh }, 200);
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      return jsonResponse({ slots: [] }, 200);
    }
    await sleep(Math.min(SLOT_LIST_POLL_INTERVAL_MS, remaining));
  }
}

interface SlotIndex {
  slot_id: string;
  created_at: number;
}

async function readSlotsSince(
  bucket: R2Bucket,
  feedId: string,
  sinceTs: number,
): Promise<SlotIndex[]> {
  const prefix = `feeds/${feedId}/slots/`;
  const list = await bucket.list({ prefix, limit: 1000 });
  const out: SlotIndex[] = [];
  for (const obj of list.objects) {
    const head = await bucket.head(obj.key);
    const createdAt = parseTs(head?.customMetadata?.created_at);
    if (createdAt === null || createdAt <= sinceTs) continue;
    out.push({ slot_id: obj.key.slice(prefix.length), created_at: createdAt });
  }
  out.sort((a, b) => a.created_at - b.created_at);
  return out;
}

// ---------- helpers ----------

function generateFeedId(): string {
  // 9 random bytes = 12 base64url chars, ~72 bits. Matches Python
  // secrets.token_urlsafe(9).
  const bytes = new Uint8Array(9);
  crypto.getRandomValues(bytes);
  return urlsafeBase64Encode(bytes);
}

function urlsafeBase64Encode(bytes: Uint8Array): string {
  let bin = "";
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function clampTtl(input: unknown): number {
  if (typeof input !== "number" || !Number.isFinite(input)) {
    return DEFAULT_TTL_SEC;
  }
  const v = Math.floor(input);
  if (v < MIN_TTL_SEC) return MIN_TTL_SEC;
  if (v > MAX_TTL_SEC) return MAX_TTL_SEC;
  return v;
}

function isExpired(meta: Record<string, string> | undefined): boolean {
  const exp = parseTs(meta?.expires_at);
  if (exp === null) return false;
  return exp < Math.floor(Date.now() / 1000);
}

function parseTs(s: string | undefined): number | null {
  if (s === undefined) return null;
  const n = Number.parseInt(s, 10);
  return Number.isFinite(n) ? n : null;
}

async function readExpiresAt(
  bucket: R2Bucket,
  feedId: string,
): Promise<number | null> {
  const head = await bucket.head(`feeds/${feedId}/meta`);
  return parseTs(head?.customMetadata?.expires_at);
}

async function ensureFeedLive(
  bucket: R2Bucket,
  feedId: string,
): Promise<Response> {
  const head = await bucket.head(`feeds/${feedId}/meta`);
  if (head === null) {
    return new Response("feed-not-found", { status: 404 });
  }
  if (isExpired(head.customMetadata)) {
    await purgeFeed(bucket, feedId);
    return new Response("feed-expired", { status: 410 });
  }
  return new Response(null, { status: 200 });
}

async function purgeFeed(bucket: R2Bucket, feedId: string): Promise<void> {
  const prefix = `feeds/${feedId}/`;
  let cursor: string | undefined;
  do {
    const list = await bucket.list({ prefix, limit: 1000, cursor });
    if (list.objects.length === 0) break;
    await Promise.all(list.objects.map((o) => bucket.delete(o.key)));
    cursor = list.truncated ? list.cursor : undefined;
  } while (cursor !== undefined);
}

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
