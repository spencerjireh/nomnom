// Feed lifecycle and storage on R2.
//
// Storage layout (prefix: feeds/<feed_id>/):
//   feeds/<id>/meta              — JSON {created_at, expires_at}
//   feeds/<id>/members/<mid>     — JSON member card
//   feeds/<id>/slots/<slot_id>   — raw ciphertext (encrypted file)
//
// All objects carry `customMetadata.expires_at` matching the feed's expiry.
//
// Physical cleanup is the R2 bucket lifecycle's job. That rule expires objects
// by AGE SINCE LAST WRITE, not last access, so on its own it would purge a
// permanent channel a fixed time after minting even while it's in active use.
// To turn it into "purge after N days of INACTIVITY", every feed operation
// re-writes `feeds/<id>/meta` (throttled to once/day) so its R2 age tracks last
// use — see `maybeTouchFeed`. Slots/member cards are intentionally left to age
// out: a sent file or a stale roster card lapsing after the same window is the
// desired "kept for up to a month" behaviour, and the channel itself (meta)
// survives as long as any device keeps using it.
//
// Auth shape:
//   POST /feeds                  — relay HMAC (gates who can mint feeds)
//   /feeds/:id/*                 — feed-key signature (URL token IS the credential)

import { pollSlot } from "./poll";
import { errorResponse, jsonResponse, sleep } from "./http";
import { urlsafeBase64Encode } from "./crypto-util";
import {
  LIST_BATCH_SIZE,
  SLOT_HEAD_CONCURRENCY,
  SlotIndex,
  parseTs,
} from "./slot-index";

const FEED_ID_RE = /^[A-Za-z0-9_-]{8,32}$/;
const MEMBER_ID_RE = /^[A-Za-z0-9_-]{8,64}$/;
const SLOT_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

const DEFAULT_TTL_SEC = 86_400; // 1 day
// A "channel" is one permanent feed shared across a user's own devices, so the
// cap is effectively forever (10 years). `nomnom init` mints with a multi-year
// TTL; this keeps the channel alive even when every device is idle for months.
const MAX_TTL_SEC = 3650 * 86_400; // ~10 years
const MIN_TTL_SEC = 60; // 1 minute (sanity floor)
// Re-write the feed's meta at most this often so its R2 object age tracks last
// use without a write on every request. The bucket lifecycle deletes objects
// this-rule-many days after their last write; touching meta on use makes that
// "days of inactivity" rather than "days since minting". One write/day per
// active feed is a negligible cost.
const TOUCH_THROTTLE_SEC = 86_400; // 1 day
const MAX_MEMBER_CARD_BYTES = 4096;
const MAX_MEMBER_NAME_LEN = 128;
const MAX_BODY_BYTES = 256 * 1024 * 1024; // 256 MB
const MAX_MEMBER_COUNT = 64; // bound roster size per feed

const MEMBER_POLL_INTERVAL_MS = 1000;
const SLOT_LIST_POLL_INTERVAL_MS = 1000;
const MAX_BUDGET_MS = 30_000;

function byteLength(s: string): number {
  return new TextEncoder().encode(s).length;
}

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
  if (byteLength(cardJson) > MAX_MEMBER_CARD_BYTES) {
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
  ctx?: ExecutionContext,
): Promise<Response> {
  const obj = await bucket.get(`feeds/${feedId}/meta`);
  if (obj === null) {
    return errorResponse("feed-not-found", 404);
  }
  if (isExpired(obj.customMetadata)) {
    await purgeFeed(bucket, feedId);
    return errorResponse("feed-expired", 410);
  }
  maybeTouchFeed(bucket, feedId, obj.uploaded, ctx);
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
  ctx?: ExecutionContext,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId, ctx);
  if (!live.ok) return live.res;

  const text = await req.text();
  if (byteLength(text) > MAX_MEMBER_CARD_BYTES) {
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

  const now = Math.floor(Date.now() / 1000);
  await bucket.put(`feeds/${feedId}/members/${memberId}`, text, {
    customMetadata: {
      expires_at: String(live.expiresAt ?? now + DEFAULT_TTL_SEC),
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

interface MemberCard {
  member_id: string;
  identity_pubkey: string;
  name: string;
  joined_at: number;
}

export async function listMembers(
  bucket: R2Bucket,
  feedId: string,
  waitMs: number,
  sinceTs: number,
  ctx?: ExecutionContext,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId, ctx);
  if (!live.ok) return live.res;

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

// ---------- PUT /feeds/:id/slots/:slot_id ----------

export async function putFeedSlot(
  bucket: R2Bucket,
  feedId: string,
  slotId: string,
  req: Request,
  notifier?: DurableObjectNamespace,
  ctx?: ExecutionContext,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId, ctx);
  if (!live.ok) return live.res;

  const lenHdr = req.headers.get("Content-Length");
  if (lenHdr !== null) {
    const len = Number.parseInt(lenHdr, 10);
    if (Number.isFinite(len) && len > MAX_BODY_BYTES) {
      return errorResponse("payload-too-large", 413);
    }
  }
  if (req.body === null) {
    return errorResponse("empty-body", 400);
  }
  const key = `feeds/${feedId}/slots/${slotId}`;
  const now = Math.floor(Date.now() / 1000);
  const expiresAt = live.expiresAt ?? now + DEFAULT_TTL_SEC;
  // Atomic create-if-absent: R2 returns null when the precondition fails, so a
  // racing PUT to the same slot id gets 409 instead of silently clobbering.
  const created = await bucket.put(key, req.body, {
    onlyIf: { etagDoesNotMatch: "*" },
    customMetadata: {
      expires_at: String(expiresAt),
      created_at: String(now),
    },
  });
  if (created === null) {
    return errorResponse("slot-occupied", 409);
  }

  // Best-effort push: nudge the feed's notifier DO so any open SSE streams get
  // the new slot immediately. Receivers also poll/replay, so a missed signal is
  // a latency hit, not a correctness one — hence waitUntil + swallowed errors.
  if (notifier && ctx) {
    const stub = notifier.get(notifier.idFromName(feedId));
    ctx.waitUntil(
      stub
        .fetch("https://feed-notifier/notify", {
          method: "POST",
          body: JSON.stringify({ slot_id: slotId, created_at: now }),
        })
        .then(() => undefined)
        .catch(() => undefined),
    );
  }
  return new Response(null, { status: 204 });
}

// ---------- GET /feeds/:id/slots/:slot_id ----------
// Multi-party broadcast: NO delete-on-read. Slot lives until feed TTL.

export async function getFeedSlot(
  bucket: R2Bucket,
  feedId: string,
  slotId: string,
  waitMs: number,
  ctx?: ExecutionContext,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId, ctx);
  if (!live.ok) return live.res;

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
  ctx?: ExecutionContext,
): Promise<Response> {
  const live = await ensureFeedLive(bucket, feedId, ctx);
  if (!live.ok) return live.res;

  const prefix = `feeds/${feedId}/slots/`;
  // Iterations are bounded by `deadline` (MAX_BUDGET_MS) over the poll interval,
  // so the per-request subrequest count stays well under the free-tier cap. The
  // liveness gate above is connect-time only; a feed closed mid-poll keeps
  // serving until the deadline, which is acceptable for ~permanent channels.
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

type FeedLive =
  | { ok: true; expiresAt: number | null }
  | { ok: false; res: Response };

// Single meta `head` that both gates liveness and yields the feed's expiry, so
// the write paths don't head the same object twice.
async function ensureFeedLive(
  bucket: R2Bucket,
  feedId: string,
  ctx?: ExecutionContext,
): Promise<FeedLive> {
  const head = await bucket.head(`feeds/${feedId}/meta`);
  if (head === null) {
    return { ok: false, res: errorResponse("feed-not-found", 404) };
  }
  if (isExpired(head.customMetadata)) {
    await purgeFeed(bucket, feedId);
    return { ok: false, res: errorResponse("feed-expired", 410) };
  }
  maybeTouchFeed(bucket, feedId, head.uploaded, ctx);
  return { ok: true, expiresAt: parseTs(head.customMetadata?.expires_at) };
}

// Reset the meta object's R2 age when the feed is used, so the bucket's
// age-based lifecycle rule purges by inactivity rather than by mint time.
// Throttled: skip unless meta hasn't been re-written for TOUCH_THROTTLE_SEC.
// The re-write is a byte-identical copy of meta — its only effect is bumping
// the object's `uploaded` timestamp. Fire-and-forget via waitUntil so it never
// adds latency to the request; errors are swallowed (a missed touch only risks
// an earlier purge, which the next use re-arms).
function maybeTouchFeed(
  bucket: R2Bucket,
  feedId: string,
  uploaded: Date,
  ctx?: ExecutionContext,
): void {
  if ((Date.now() - uploaded.getTime()) / 1000 < TOUCH_THROTTLE_SEC) return;
  const work = touchFeedMeta(bucket, feedId).catch(() => undefined);
  // Without an ExecutionContext the runtime may cancel the write after the
  // response is sent, so only fire it when we can keep it alive.
  if (ctx) ctx.waitUntil(work);
}

async function touchFeedMeta(bucket: R2Bucket, feedId: string): Promise<void> {
  const key = `feeds/${feedId}/meta`;
  const obj = await bucket.get(key);
  if (obj === null) return;
  const body = await obj.text();
  // Re-derive customMetadata from the body (the source of truth) so a touch of
  // legacy meta lacking customMetadata still re-writes a complete object.
  let createdAt = parseTs(obj.customMetadata?.created_at);
  let expiresAt = parseTs(obj.customMetadata?.expires_at);
  try {
    const parsed = JSON.parse(body) as { created_at?: unknown; expires_at?: unknown };
    if (createdAt === null) createdAt = parseTs(String(parsed.created_at));
    if (expiresAt === null) expiresAt = parseTs(String(parsed.expires_at));
  } catch {
    return; // unparseable meta — don't risk rewriting it
  }
  if (expiresAt === null) return;
  const customMetadata: Record<string, string> = { expires_at: String(expiresAt) };
  if (createdAt !== null) customMetadata.created_at = String(createdAt);
  await bucket.put(key, body, {
    customMetadata,
    httpMetadata: { contentType: "application/json" },
  });
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
