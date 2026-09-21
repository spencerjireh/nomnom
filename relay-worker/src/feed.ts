// Per-feed Durable Object: the single owner of everything about one feed.
//
// SQLite holds the post index (a monotonic `seq` cursor), the member roster,
// and the feed's own bookkeeping. R2 holds ciphertext bodies only, keyed
// feeds/<id>/slots/<slot_id>; nothing ever lists the bucket. The object also
// owns the live connections — hibernatable WebSockets for the web client and
// awaited long-polls for the CLI — and a daily alarm that enforces the 30-day
// retention window.
//
// Tables (created by `create()` only; never lazily anywhere else):
//   feed    (feed_id, created_at, last_used_at)   single row
//   members (member_id PK, card, identity_pubkey, name, joined_at)
//   posts   (seq AUTOINCREMENT PK, slot_id UNIQUE, created_at, size)
//
// `feed_id` is stored because `ctx.id.name` is undefined inside the object and
// `alarm()` needs it to build R2 keys with no request in hand. AUTOINCREMENT
// (not bare rowid) so a deleted max-seq is never reused: a client whose cursor
// sits on that seq would otherwise miss the next post.
//
// Expected failures cross the RPC boundary as `DoResult` (throws lose their
// class over RPC and the Worker maps them to 502).

import { DurableObject } from "cloudflare:workers";
import { errorResponse, MAX_BUDGET_MS, parseSince } from "./http";
import {
  ALARM_PERIOD_MS,
  FEED_IDLE_SEC,
  MAX_MEMBER_COUNT,
  POST_TTL_SEC,
  TOUCH_THROTTLE_SEC,
  fail,
  ok,
  slotKey,
  type DoResult,
  type MemberCard,
  type MemberRow,
  type PostRow,
  type WsFrame,
} from "./feed-types";

interface FeedEnv {
  BUCKET: R2Bucket;
}

interface Waiter {
  resolve: () => void;
  timer: ReturnType<typeof setTimeout>;
}

// R2 bulk delete accepts up to this many keys per call.
const R2_DELETE_BATCH = 1000;

function nowSec(): number {
  return Math.floor(Date.now() / 1000);
}

// `getWebSockets()` can hand back sockets already in CLOSING/CLOSED.
function closeQuietly(ws: WebSocket, code: number, reason: string): void {
  try {
    ws.close(code, reason);
  } catch {
    // already closed
  }
}

export class Feed extends DurableObject<FeedEnv> {
  // True once `create()` has run and until a purge. Every entry point 404s
  // when false and writes nothing, so a stray authed request to a never-minted
  // or already-purged id leaves zero storage behind.
  private live: boolean;
  private feedId = "";
  private createdAt = 0;
  private lastUsedAt = 0;
  // Long-poll waiters. Only meaningful while an RPC is in flight, which blocks
  // hibernation, so losing them on eviction is safe (the caller's RPC rejects
  // and its retry loop reconnects).
  private slotWaiters = new Set<Waiter>();
  private memberWaiters = new Set<Waiter>();
  // Slot ids with an R2 put or delete in flight. DO input gates serialise
  // storage ops but NOT R2 I/O, so two PUTs to one id can interleave across
  // `await bucket.put`; the loser must be refused before it touches R2 or its
  // cleanup would delete the winner's blob.
  private pendingSlots = new Set<string>();

  constructor(ctx: DurableObjectState, env: FeedEnv) {
    super(ctx, env);
    // One statement per wake: the feed row exists iff `create()` ran, so a
    // "no such table" throw is the not-live signal.
    let row: { feed_id: string; created_at: number; last_used_at: number } | null;
    try {
      row = ctx.storage.sql
        .exec<{ feed_id: string; created_at: number; last_used_at: number }>(
          "SELECT feed_id, created_at, last_used_at FROM feed",
        )
        .one();
    } catch {
      row = null;
    }
    this.live = row !== null;
    if (row) {
      this.feedId = row.feed_id;
      this.createdAt = row.created_at;
      this.lastUsedAt = row.last_used_at;
    }
    // Answer client keepalives at the edge without waking the object.
    ctx.setWebSocketAutoResponse(new WebSocketRequestResponsePair("ping", "pong"));
  }

  private get sql(): SqlStorage {
    return this.ctx.storage.sql;
  }

  // ---------- RPC: feed lifecycle ----------

  async create(
    feedId: string,
    card: MemberCard,
    cardJson: string,
  ): Promise<DoResult<{ created_at: number }>> {
    if (this.live) return fail(409, "feed-exists");
    const now = nowSec();
    this.ctx.storage.transactionSync(() => {
      this.sql.exec(
        `CREATE TABLE feed (
           feed_id TEXT NOT NULL,
           created_at INTEGER NOT NULL,
           last_used_at INTEGER NOT NULL
         )`,
      );
      this.sql.exec(
        `CREATE TABLE members (
           member_id TEXT PRIMARY KEY,
           card TEXT NOT NULL,
           identity_pubkey TEXT NOT NULL,
           name TEXT NOT NULL,
           joined_at INTEGER NOT NULL
         )`,
      );
      this.sql.exec(
        `CREATE TABLE posts (
           seq INTEGER PRIMARY KEY AUTOINCREMENT,
           slot_id TEXT NOT NULL UNIQUE,
           created_at INTEGER NOT NULL,
           size INTEGER NOT NULL
         )`,
      );
      // No index on created_at on purpose: its only reader is the daily
      // retention sweep over at most 30 days of rows, and an index would cost
      // a row write per insert and per delete for that one scan.
      this.sql.exec(
        "INSERT INTO feed (feed_id, created_at, last_used_at) VALUES (?, ?, ?)",
        feedId,
        now,
        now,
      );
      this.sql.exec(
        "INSERT INTO members (member_id, card, identity_pubkey, name, joined_at) VALUES (?, ?, ?, ?, ?)",
        card.member_id,
        cardJson,
        card.identity_pubkey,
        card.name,
        now,
      );
    });
    await this.ctx.storage.setAlarm(Date.now() + ALARM_PERIOD_MS);
    this.live = true;
    this.feedId = feedId;
    this.createdAt = now;
    this.lastUsedAt = now;
    return ok({ created_at: now });
  }

  async meta(): Promise<DoResult<{ created_at: number; last_used_at: number }>> {
    if (!this.live) return fail(404, "feed-not-found");
    this.touch(nowSec());
    return ok({ created_at: this.createdAt, last_used_at: this.lastUsedAt });
  }

  // DELETE /feeds/:id — everything goes: rows, blobs, sockets, alarm.
  async purge(): Promise<DoResult<null>> {
    if (!this.live) return fail(404, "feed-not-found");
    await this.purgeStorage();
    return ok(null);
  }

  // ---------- RPC: members ----------

  async putMember(card: MemberCard, cardJson: string): Promise<DoResult<null>> {
    if (!this.live) return fail(404, "feed-not-found");
    const now = nowSec();
    this.touch(now);
    // One scan (the roster is at most 64 rows) answers both "is this an
    // update" and "is there room for a new member".
    const { n, known } = this.sql
      .exec<{ n: number; known: number }>(
        "SELECT COUNT(*) AS n, COALESCE(SUM(member_id = ?), 0) AS known FROM members",
        card.member_id,
      )
      .one();
    if (!known && n >= MAX_MEMBER_COUNT) return fail(409, "feed-full");
    // A re-PUT (rename/rekey) overwrites the card and bumps joined_at, which is
    // what wakes a members long-poll and what the `fresh` filter keys on.
    this.sql.exec(
      `INSERT INTO members (member_id, card, identity_pubkey, name, joined_at)
       VALUES (?, ?, ?, ?, ?)
       ON CONFLICT(member_id) DO UPDATE SET
         card = excluded.card,
         identity_pubkey = excluded.identity_pubkey,
         name = excluded.name,
         joined_at = excluded.joined_at`,
      card.member_id,
      cardJson,
      card.identity_pubkey,
      card.name,
      now,
    );
    this.broadcast({
      type: "member",
      action: "join",
      member: { ...card, joined_at: now },
    });
    this.wake(this.memberWaiters);
    return ok(null);
  }

  async deleteMember(memberId: string): Promise<DoResult<null>> {
    if (!this.live) return fail(404, "feed-not-found");
    this.touch(nowSec());
    const gone = this.sql
      .exec<MemberRow>(
        "DELETE FROM members WHERE member_id = ? RETURNING member_id, identity_pubkey, name, joined_at",
        memberId,
      )
      .toArray();
    if (gone.length > 0) {
      this.broadcast({ type: "member", action: "leave", member: gone[0] });
    }
    return ok(null);
  }

  // `since` is a joined_at timestamp: the response always carries the full
  // roster, `fresh` is only the wake-up hint for a long-poll.
  async listMembers(
    sinceTs: number,
    waitMs: number,
  ): Promise<DoResult<{ members: MemberRow[]; fresh: MemberRow[] }>> {
    const members = await this.longPoll(
      this.memberWaiters,
      waitMs,
      () => this.queryMembers(),
      (rows) => rows.some((m) => m.joined_at > sinceTs),
    );
    if (members === null) return fail(404, "feed-not-found");
    return ok({ members, fresh: members.filter((m) => m.joined_at > sinceTs) });
  }

  private queryMembers(): MemberRow[] {
    return this.sql
      .exec<MemberRow>(
        "SELECT member_id, identity_pubkey, name, joined_at FROM members ORDER BY joined_at, member_id",
      )
      .toArray();
  }

  // ---------- RPC: posts ----------

  async listSlots(
    sinceSeq: number,
    waitMs: number,
  ): Promise<DoResult<{ slots: PostRow[] }>> {
    const slots = await this.longPoll(
      this.slotWaiters,
      waitMs,
      () => this.querySlots(sinceSeq),
      (rows) => rows.length > 0,
    );
    if (slots === null) return fail(404, "feed-not-found");
    return ok({ slots });
  }

  // Shared long-poll shape: query; if nothing satisfies `ready` and the caller
  // will wait, park until a writer wakes the set or the deadline passes, then
  // query once more. A wake always coincides with a committed write, and the
  // deadline path returns whatever is there (the client re-polls). Returns
  // null if the feed is not live (including a purge during the wait).
  private async longPoll<T>(
    set: Set<Waiter>,
    waitMs: number,
    query: () => T[],
    ready: (rows: T[]) => boolean,
  ): Promise<T[] | null> {
    if (!this.live) return null;
    this.touch(nowSec());
    let rows = query();
    if (!ready(rows) && waitMs > 0) {
      await this.waitFor(set, waitMs);
      if (!this.live) return null;
      rows = query();
    }
    return rows;
  }

  private querySlots(sinceSeq: number): PostRow[] {
    return this.sql
      .exec<PostRow>(
        "SELECT seq, slot_id, created_at FROM posts WHERE seq > ? ORDER BY seq",
        sinceSeq,
      )
      .toArray();
  }

  async deletePost(slotId: string): Promise<DoResult<null>> {
    if (!this.live) return fail(404, "feed-not-found");
    this.touch(nowSec());
    if (this.pendingSlots.has(slotId)) return fail(409, "slot-busy");
    const gone = this.sql
      .exec("DELETE FROM posts WHERE slot_id = ? RETURNING seq", slotId)
      .toArray();
    if (gone.length === 0) return fail(404, "not-found");
    // Hold the id while the blob delete is in flight so a concurrent re-PUT of
    // the same id cannot land between the row delete and the blob delete.
    this.pendingSlots.add(slotId);
    try {
      await this.env.BUCKET.delete(this.slotKey(slotId));
    } finally {
      this.pendingSlots.delete(slotId);
    }
    this.broadcast({ type: "deleted", slot_id: slotId });
    return ok(null);
  }

  // ---------- fetch(): only what cannot cross RPC ----------
  // The upload body is a stream and the WebSocket upgrade is a Response, so
  // these two ride the stub's fetch(). The Worker has already authenticated
  // and validated ids; paths here are internal.

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    const m = url.pathname.match(/^\/slots\/([^/]+)$/);
    if (req.method === "PUT" && m) return this.handlePutSlot(m[1], req);
    if (req.method === "GET" && url.pathname === "/ws") {
      return this.handleWs(req, url);
    }
    return errorResponse("not-found", 404);
  }

  private async handlePutSlot(slotId: string, req: Request): Promise<Response> {
    if (!this.live) return errorResponse("feed-not-found", 404);
    const now = nowSec();
    this.touch(now);
    // Everything up to the first await is one synchronous block: the duplicate
    // guard must run before any R2 I/O (see `pendingSlots`).
    if (
      this.pendingSlots.has(slotId) ||
      this.sql.exec("SELECT 1 FROM posts WHERE slot_id = ?", slotId).toArray()
        .length > 0
    ) {
      return errorResponse("slot-occupied", 409);
    }
    // The Worker validated Content-Length (`rejectBody`) and forwarded the
    // original request, so the length is known here (R2 needs it) and the
    // body is present.
    const len = Number(req.headers.get("Content-Length"));
    const key = this.slotKey(slotId);
    this.pendingSlots.add(slotId);
    let created: R2Object | null;
    try {
      created = await this.env.BUCKET.put(key, req.body!, {
        onlyIf: { etagDoesNotMatch: "*" },
      });
    } finally {
      this.pendingSlots.delete(slotId);
    }
    if (created === null) {
      // A blob already exists under this key (a previous incarnation's row was
      // lost, or a purge is racing). Do not delete it; refuse the write.
      return errorResponse("slot-occupied", 409);
    }
    let seq: number;
    try {
      const row = this.sql
        .exec<{ seq: number }>(
          "INSERT INTO posts (slot_id, created_at, size) VALUES (?, ?, ?) RETURNING seq",
          slotId,
          now,
          len,
        )
        .one();
      seq = row.seq;
    } catch {
      await this.env.BUCKET.delete(key).catch(() => undefined);
      return errorResponse("post-index-failed", 500);
    }
    this.broadcast({ type: "post", seq, slot_id: slotId, created_at: now });
    this.wake(this.slotWaiters);
    return new Response(null, { status: 204 });
  }

  private handleWs(_req: Request, url: URL): Response {
    if (!this.live) return errorResponse("feed-not-found", 404);
    // The Worker already required `Upgrade: websocket`.
    const since = parseSince(url.searchParams.get("since"));
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    // Accept -> query -> send is one synchronous run, so a post landing "during"
    // replay is impossible: by the time any other event runs, `server` is in
    // getWebSockets() and gets the live broadcast.
    this.ctx.acceptWebSocket(server);
    for (const p of this.querySlots(since)) {
      server.send(JSON.stringify({ type: "post", ...p } satisfies WsFrame));
    }
    this.touch(nowSec());
    return new Response(null, { status: 101, webSocket: client });
  }

  // ---------- Hibernation API ----------

  async webSocketMessage(): Promise<void> {
    // Clients only send "ping", which the edge auto-answers without waking us.
    // Anything else is ignored.
  }

  async webSocketClose(ws: WebSocket, code: number, reason: string): Promise<void> {
    closeQuietly(ws, code, reason);
  }

  async webSocketError(ws: WebSocket): Promise<void> {
    closeQuietly(ws, 1011, "error");
  }

  private broadcast(frame: WsFrame): void {
    const data = JSON.stringify(frame);
    // Always read the live set from the runtime; a cached Set<WebSocket> would
    // not survive hibernation. The set may include CLOSING sockets, hence the
    // try/catch around send.
    for (const ws of this.ctx.getWebSockets()) {
      try {
        ws.send(data);
      } catch {
        // closing / closed
      }
    }
  }

  // ---------- alarm: retention ----------

  async alarm(): Promise<void> {
    if (!this.live) return;
    const now = nowSec();
    // Rows go first, then blobs: a crash in between leaves an unreachable blob
    // (nothing lists the bucket), never a row pointing at nothing. Rows already
    // gone means a retried alarm cannot double-count.
    const stale = this.sql
      .exec<{ slot_id: string }>(
        "DELETE FROM posts WHERE created_at < ? RETURNING slot_id",
        now - POST_TTL_SEC,
      )
      .toArray();
    await this.deleteBlobs(stale.map((r) => r.slot_id));
    // Cheap check first (in memory), then a one-row probe instead of a count.
    if (
      this.lastUsedAt < now - FEED_IDLE_SEC &&
      this.sql.exec("SELECT 1 FROM posts LIMIT 1").toArray().length === 0
    ) {
      await this.purgeStorage();
      return;
    }
    // Last statement on purpose: if anything above threw, the runtime retries
    // the alarm and the sweep re-runs.
    await this.ctx.storage.setAlarm(Date.now() + ALARM_PERIOD_MS);
  }

  // ---------- helpers ----------

  private slotKey(slotId: string): string {
    return slotKey(this.feedId, slotId);
  }

  // Reset the idle clock, at most once per TOUCH_THROTTLE_SEC. Downloads skip
  // this (they never reach the DO); every download is preceded by a list or a
  // /ws connect that does not.
  private touch(now: number): void {
    if (now - this.lastUsedAt < TOUCH_THROTTLE_SEC) return;
    this.sql.exec("UPDATE feed SET last_used_at = ?", now);
    this.lastUsedAt = now;
  }

  private waitFor(set: Set<Waiter>, waitMs: number): Promise<void> {
    const ms = Math.min(Math.max(waitMs, 0), MAX_BUDGET_MS);
    return new Promise<void>((resolve) => {
      const w: Waiter = {
        resolve,
        timer: setTimeout(() => {
          set.delete(w);
          resolve();
        }, ms),
      };
      set.add(w);
    });
  }

  private wake(set: Set<Waiter>): void {
    for (const w of set) {
      clearTimeout(w.timer);
      w.resolve();
    }
    set.clear();
  }

  private async deleteBlobs(slotIds: string[]): Promise<void> {
    for (let i = 0; i < slotIds.length; i += R2_DELETE_BATCH) {
      const keys = slotIds.slice(i, i + R2_DELETE_BATCH).map((id) => this.slotKey(id));
      await this.env.BUCKET.delete(keys);
    }
  }

  private async purgeStorage(): Promise<void> {
    const slotIds = this.sql
      .exec<{ slot_id: string }>("SELECT slot_id FROM posts")
      .toArray()
      .map((r) => r.slot_id);
    for (const ws of this.ctx.getWebSockets()) closeQuietly(ws, 1000, "feed-deleted");
    // Flip before the waiters run so they see a dead feed on re-query.
    this.live = false;
    this.wake(this.slotWaiters);
    this.wake(this.memberWaiters);
    await this.deleteBlobs(slotIds);
    await this.ctx.storage.deleteAll();
    await this.ctx.storage.deleteAlarm();
  }
}
