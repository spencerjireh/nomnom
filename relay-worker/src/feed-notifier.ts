// Durable Object: per-feed Server-Sent Events notifier.
//
// One instance per feed (addressed via idFromName(feedId)). It holds the set of
// open `text/event-stream` connections and pushes a tiny `{slot_id, created_at}`
// frame to all of them when `putFeedSlot` signals a new post — replacing the
// receivers' R2 long-poll with real push. The DO never sees plaintext or
// ciphertext: clients still GET /feeds/:id/slots/:slot_id for the blob.
//
// Internal verbs (the public worker fronts these after auth):
//   GET  /connect?feed=<id>&since=<ts>   open a stream; replay backlog; subscribe
//   POST /notify  {slot_id, created_at}  broadcast to all open streams
//
// The stream self-closes at STREAM_MAX_MS so clients reconnect with a freshly
// signed URL — keeping each connection's `?auth` timestamp inside the relay's
// ±300s window (EventSource auto-reconnects to the same, eventually-stale URL).

import { errorResponse, parseSinceTs } from "./http";
import { TIMESTAMP_WINDOW_SEC } from "./crypto-util";
import { readSlotsSince } from "./slot-index";

const HEARTBEAT_MS = 20_000;
// Self-close a minute inside the auth window so a reconnect always re-signs in
// time (derived from the window so the two can't silently drift apart).
const STREAM_MAX_MS = (TIMESTAMP_WINDOW_SEC - 60) * 1000;

interface NotifierEnv {
  BUCKET: R2Bucket;
}

interface Conn {
  controller: ReadableStreamDefaultController<Uint8Array>;
  heartbeat: ReturnType<typeof setInterval>;
  closer: ReturnType<typeof setTimeout>;
  closed: boolean;
}

export class FeedNotifier {
  private conns = new Set<Conn>();
  private enc = new TextEncoder();

  // `state` is unused (no DO storage — connections are in-memory and the
  // backlog comes from R2), but the runtime passes it to the constructor.
  constructor(_state: DurableObjectState, private env: NotifierEnv) {}

  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/notify" && req.method === "POST") {
      return this.handleNotify(req);
    }
    if (url.pathname === "/connect" && req.method === "GET") {
      return this.handleConnect(url);
    }
    return errorResponse("not-found", 404);
  }

  private async handleNotify(req: Request): Promise<Response> {
    let body: { slot_id?: unknown; created_at?: unknown };
    try {
      body = await req.json();
    } catch {
      return errorResponse("bad-json", 400);
    }
    if (
      typeof body.slot_id === "string" &&
      typeof body.created_at === "number"
    ) {
      this.broadcast(body.slot_id, body.created_at);
    }
    return new Response(null, { status: 204 });
  }

  private async handleConnect(url: URL): Promise<Response> {
    const feedId = url.searchParams.get("feed") ?? "";
    const sinceTs = parseSinceTs(url.searchParams.get("since"));

    // `start` runs synchronously during construction, so `conn` is set before
    // we return. Subscribe BEFORE replaying the backlog so a post arriving
    // mid-replay is delivered live rather than dropped.
    let conn!: Conn;
    const stream = new ReadableStream<Uint8Array>({
      start: (controller) => {
        conn = {
          controller,
          heartbeat: setInterval(
            () => this.write(conn, ": ping\n\n"),
            HEARTBEAT_MS,
          ),
          closer: setTimeout(() => this.close(conn), STREAM_MAX_MS),
          closed: false,
        };
        this.conns.add(conn);
        this.write(conn, ": ok\n\n"); // flush headers to the client
      },
      cancel: () => this.close(conn),
    });

    await this.replayBacklog(feedId, sinceTs, conn);

    return new Response(stream, {
      status: 200,
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
      },
    });
  }

  // Replay slots written after `sinceTs` so a (re)connecting client catches up
  // on anything it missed while disconnected. Clients also dedup by cursor, so
  // overlap with a concurrent live notify is harmless.
  private async replayBacklog(
    feedId: string,
    sinceTs: number,
    conn: Conn,
  ): Promise<void> {
    if (!feedId) return;
    let fresh;
    try {
      fresh = await readSlotsSince(this.env.BUCKET, feedId, sinceTs);
    } catch {
      return;
    }
    for (const s of fresh) this.frame(conn, s.slot_id, s.created_at);
  }

  private broadcast(slotId: string, createdAt: number): void {
    for (const conn of this.conns) this.frame(conn, slotId, createdAt);
  }

  private frame(conn: Conn, slotId: string, createdAt: number): void {
    const data = JSON.stringify({ slot_id: slotId, created_at: createdAt });
    // The slot id is the collision-free event id; created_at (seconds) would
    // collide for two posts in the same second if Last-Event-ID is ever wired up.
    this.write(conn, `id: ${slotId}\ndata: ${data}\n\n`);
  }

  private write(conn: Conn, s: string): void {
    if (conn.closed) return;
    try {
      conn.controller.enqueue(this.enc.encode(s));
    } catch {
      this.close(conn);
    }
  }

  private close(conn: Conn): void {
    if (conn.closed) return;
    conn.closed = true;
    clearInterval(conn.heartbeat);
    clearTimeout(conn.closer);
    this.conns.delete(conn);
    try {
      conn.controller.close();
    } catch {
      // already closed/errored
    }
  }
}
