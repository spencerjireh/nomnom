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
import { errorResponse, jsonResponse, sleep } from "./http";
import { urlsafeBase64Encode } from "./crypto-util";

const FEED_ID_RE = /^[A-Za-z0-9_-]{8,32}$/;
const MEMBER_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
const SLOT_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

const DEFAULT_TTL_SEC = 86_400; // 1 day
const MAX_TTL_SEC = 90 * 86_400; // 90 days
const MIN_TTL_SEC = 60; // 1 minute (sanity floor)
const MAX_MEMBER_CARD_BYTES = 4096;
const MAX_MEMBER_NAME_LEN = 128;
const MAX_BODY_BYTES = 256 * 1024 * 1024; // 256 MB
const MAX_MEMBER_COUNT = 64; // bound roster size per feed

const MEMBER_POLL_INTERVAL_MS = 1000;
const SLOT_LIST_POLL_INTERVAL_MS = 1000;
const MAX_BUDGET_MS = 30_000;
const LIST_BATCH_SIZE = 1000;
// Cap on concurrent R2 sub-requests within readSlotsSince. Workers free tier
// allows 50 subrequests per invocation, paid 1000; 16 keeps us comfortable
// even with the long-poll's surrounding reads.
const SLOT_HEAD_CONCURRENCY = 16;

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
    return errorResponse("bad-json", 400);
  }

  const ttl = clampTtl(body.ttl_seconds);
  const card = body.member_card;
  if (
    !card ||
    typeof card.member_id !== "string" ||
    !validateMemberId(card.member_id) ||
    typeof card.identity_pubkey !== "string" ||
    typeof card.name !== "string" ||
    card.name.length > MAX_MEMBER_NAME_LEN
  ) {
    return errorResponse("bad-member-card", 400);
  }
  const cardJson = JSON.stringify(card);
  if (cardJson.length > MAX_MEMBER_CARD_BYTES) {
    return errorResponse("member-card-too-large", 413);
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
    return errorResponse("feed-not-found", 404);
  }
  if (isExpired(obj.customMetadata)) {
    await purgeFeed(bucket, feedId);
    return errorResponse("feed-expired", 410);
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
    return errorResponse("bad-json", 400);
  }
  const ttl = clampTtl(body.new_ttl_seconds);

  const metaObj = await bucket.get(`feeds/${feedId}/meta`);
  if (metaObj === null) {
    return errorResponse("feed-not-found", 404);
  }
  if (isExpired(metaObj.customMetadata)) {
    await purgeFeed(bucket, feedId);
    return errorResponse("feed-expired", 410);
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
  // Per-object expires_at on members/slots stays at the old value — slot reads
  // intentionally only consult feed meta (via ensureFeedLive), so extending the
  // feed keeps already-posted slots reachable. R2 lifecycle cleans by age.
  return jsonResponse({ expires_at: newExpiresAt }, 200);
}

// ---------- DELETE /feeds/:id ----------

export async function closeFeed(
  bucket: R2Bucket,
  feedId: string,
): Promise<Response> {
  const head = await bucket.head(`feeds/${feedId}/meta`);
  if (head === null) {
    return errorResponse("feed-not-found", 404);
  }
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
    return errorResponse("member-card-too-large", 413);
  }
  let card: { member_id?: string; identity_pubkey?: string; name?: string };
  try {
    card = JSON.parse(text);
  } catch {
    return errorResponse("bad-json", 400);
  }
  if (
    typeof card.member_id !== "string" ||
    card.member_id !== memberId ||
    typeof card.identity_pubkey !== "string" ||
    typeof card.name !== "string" ||
    card.name.length > MAX_MEMBER_NAME_LEN
  ) {
    return errorResponse("bad-member-card", 400);
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
    return errorResponse("feed-full", 409);
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
  // Cache parsed cards across long-poll iterations keyed by R2 key.
  // Member cards are immutable per (member_id, joined_at) tuple, so once a key
  // is in this map we never need to re-fetch its body. Per iteration we issue
  // exactly ONE bucket.list to detect joins/leaves; bodies are fetched only
  // for keys we haven't seen yet. This keeps the worst-case subrequest count
  // bounded at (initial_roster_size + iteration_count) instead of
  // (roster_size * iteration_count).
  const cards = new Map<string, MemberCard>();
  while (true) {
    const list = await bucket.list({
      prefix: `feeds/${feedId}/members/`,
      limit: MAX_MEMBER_COUNT + 1,
    });
    const currentKeys = new Set(list.objects.map((o) => o.key));
    // Drop members who have left since the last iteration.
    for (const k of cards.keys()) {
      if (!currentKeys.has(k)) cards.delete(k);
    }
    // Fetch bodies only for keys we haven't seen yet.
    const newObjs = list.objects.filter((o) => !cards.has(o.key));
    if (newObjs.length > 0) {
      const bodies = await Promise.all(newObjs.map((o) => bucket.get(o.key)));
      for (let i = 0; i < newObjs.length; i++) {
        const body = bodies[i];
        if (body === null) continue;
        const joinedAt = parseTs(body.customMetadata?.created_at) ?? 0;
        let parsed: { member_id: string; identity_pubkey: string; name: string };
        try {
          parsed = JSON.parse(await body.text());
        } catch {
          continue;
        }
        cards.set(newObjs[i].key, {
          member_id: parsed.member_id,
          identity_pubkey: parsed.identity_pubkey,
          name: parsed.name,
          joined_at: joinedAt,
        });
      }
    }

    const all = [...cards.values()];
    const fresh = all.filter((m) => m.joined_at > sinceTs);
    if (fresh.length > 0 || waitMs <= 0) {
      return jsonResponse({ members: all, fresh }, 200);
    }
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      return jsonResponse({ members: all, fresh: [] }, 200);
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
      return errorResponse("payload-too-large", 413);
    }
  }
  const key = `feeds/${feedId}/slots/${slotId}`;
  const existing = await bucket.head(key);
  if (existing !== null) {
    return errorResponse("slot-occupied", 409);
  }
  if (req.body === null) {
    return errorResponse("empty-body", 400);
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
    return errorResponse("not-found", 404);
  }
  // The feed-level meta (checked by ensureFeedLive above) is the authoritative
  // TTL gate. Per-slot customMetadata.expires_at is only what the feed TTL was
  // at slot-write time; it goes stale after extendFeed, so we don't consult it.
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

  const prefix = `feeds/${feedId}/slots/`;
  const deadline = Date.now() + Math.min(Math.max(waitMs, 0), MAX_BUDGET_MS);
  // Cache slot created_at across long-poll iterations keyed by R2 key. Slots
  // are immutable once written; bucket.head returns the same created_at on
  // every call. Per iteration we issue ONE bucket.list to discover NEW keys
  // and only head the ones we haven't seen — bounding the worst case at
  // (current_slot_count + iteration_count * new_keys) rather than
  // (slot_count * iteration_count) head calls.
  const seen = new Map<string, number>(); // key -> created_at
  while (true) {
    const list = await bucket.list({ prefix, limit: LIST_BATCH_SIZE });
    const newObjs = list.objects.filter((o) => !seen.has(o.key));
    // Drop departed keys (slot expired / feed extended past slot lifetime).
    const currentKeys = new Set(list.objects.map((o) => o.key));
    for (const k of seen.keys()) {
      if (!currentKeys.has(k)) seen.delete(k);
    }
    // Head only the new keys, chunked to respect the Workers subrequest cap.
    for (let i = 0; i < newObjs.length; i += SLOT_HEAD_CONCURRENCY) {
      const slice = newObjs.slice(i, i + SLOT_HEAD_CONCURRENCY);
      const heads = await Promise.all(slice.map((o) => bucket.head(o.key)));
      for (let j = 0; j < slice.length; j++) {
        const createdAt = parseTs(heads[j]?.customMetadata?.created_at);
        if (createdAt === null) continue;
        seen.set(slice[j].key, createdAt);
      }
    }

    const fresh: SlotIndex[] = [];
    for (const [key, createdAt] of seen) {
      if (createdAt <= sinceTs) continue;
      fresh.push({ slot_id: key.slice(prefix.length), created_at: createdAt });
    }
    fresh.sort((a, b) => a.created_at - b.created_at);

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

// ---------- helpers ----------

function generateFeedId(): string {
  // 9 random bytes = 12 base64url chars, ~72 bits. Matches Python
  // secrets.token_urlsafe(9).
  const bytes = new Uint8Array(9);
  crypto.getRandomValues(bytes);
  return urlsafeBase64Encode(bytes);
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
    return errorResponse("feed-not-found", 404);
  }
  if (isExpired(head.customMetadata)) {
    await purgeFeed(bucket, feedId);
    return errorResponse("feed-expired", 410);
  }
  return new Response(null, { status: 200 });
}

async function purgeFeed(bucket: R2Bucket, feedId: string): Promise<void> {
  const prefix = `feeds/${feedId}/`;
  let cursor: string | undefined;
  do {
    const list = await bucket.list({ prefix, limit: LIST_BATCH_SIZE, cursor });
    if (list.objects.length === 0) break;
    // R2Bucket.delete accepts string[] — collapses up to LIST_BATCH_SIZE
    // class-A operations into one billable op.
    await bucket.delete(list.objects.map((o) => o.key));
    cursor = list.truncated ? list.cursor : undefined;
  } while (cursor !== undefined);
}
