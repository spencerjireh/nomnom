// Entry point: fetch() handler + route dispatch.
//
// Routes:
//   /health                                          public; no auth
//   POST   /feeds                                    relay HMAC
//   GET    /feeds/:id/meta                           feed-key sig
//   POST   /feeds/:id/extend                         feed-key sig
//   DELETE /feeds/:id                                feed-key sig
//   PUT    /feeds/:id/members/:mid                   feed-key sig
//   DELETE /feeds/:id/members/:mid                   feed-key sig
//   GET    /feeds/:id/members?wait=&since=           feed-key sig (long-poll on new joins)
//   PUT    /feeds/:id/slots/:slot_id                 feed-key sig
//   GET    /feeds/:id/slots/:slot_id?wait=           feed-key sig (long-poll, no delete-on-read)
//   GET    /feeds/:id/slots?wait=&since=             feed-key sig (long-poll on new posts)
//   GET    /feeds/:id/stream?since=&auth=            feed-key sig (SSE push of new slots; auth may ride the query)
//   PUT    /slots/:slot_id                           relay HMAC (legacy, kept)
//   GET    /slots/:slot_id?wait=                     relay HMAC (legacy, delete-on-read)
//   DELETE /slots/:slot_id                           relay HMAC (legacy)
//
// CORS: the browser client (nomnom-web) sends a custom Authorization header value,
// which is NOT CORS-safelisted, so every cross-origin request is preflighted.

import { verifyHmac } from "./auth";
import { verifyFeedKey } from "./feed-auth";
import { errorResponse } from "./http";
import {
  closeFeed,
  deleteMember,
  extendFeed,
  getFeedMeta,
  getFeedSlot,
  listFeedSlots,
  listMembers,
  mintFeed,
  putFeedSlot,
  putMember,
  validateFeedId,
  validateMemberId,
  validateSlotId as validateFeedSlotId,
} from "./feeds";
import { deleteSlot, getSlot, putSlot, validateSlotId } from "./slots";
import { FeedNotifier } from "./feed-notifier";

export { FeedNotifier };

interface Env {
  BUCKET: R2Bucket;
  NOMNOM_HMAC_SECRET: string;
  FEED_NOTIFIER: DurableObjectNamespace;
}

const CORS_ORIGINS = new Set([
  "https://nomnom.spencerjireh.com",
  "http://localhost:5173",
]);

function corsHeaders(req: Request): Record<string, string> {
  const origin = req.headers.get("Origin") ?? "";
  if (!CORS_ORIGINS.has(origin)) return {};
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function withCors(res: Response, req: Request): Response {
  const cors = corsHeaders(req);
  if (Object.keys(cors).length === 0) return res;
  // Build a fresh Response rather than mutating res.headers, which throws if
  // the response's headers are immutable.
  const headers = new Headers(res.headers);
  for (const [k, v] of Object.entries(cors)) headers.set(k, v);
  return new Response(res.body, {
    status: res.status,
    statusText: res.statusText,
    headers,
  });
}

function parseWaitMs(url: URL): number {
  const s = url.searchParams.get("wait") ?? "0";
  const n = Number.parseInt(s, 10);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

function parseSinceTs(url: URL): number {
  const s = url.searchParams.get("since") ?? "0";
  const n = Number.parseInt(s, 10);
  return Number.isFinite(n) && n >= 0 ? n : 0;
}

// A matched /feeds/:id/* route. `guard` (optional) validates a path capture and
// short-circuits with an error before any method handler runs. `Allow` for 405s
// is derived from `methods`, so it can't drift from the registered handlers.
interface FeedRoute {
  re: RegExp;
  guard?: (m: RegExpMatchArray) => Response | null;
  methods: Record<string, (m: RegExpMatchArray) => Promise<Response>>;
}

async function routeFeed(
  env: Env,
  ctx: ExecutionContext,
  feedId: string,
  subpath: string,
  req: Request,
  url: URL,
): Promise<Response> {
  const bucket = env.BUCKET;
  const routes: FeedRoute[] = [
    { re: /^\/?$/, methods: { DELETE: () => closeFeed(bucket, feedId) } },
    { re: /^\/meta$/, methods: { GET: () => getFeedMeta(bucket, feedId) } },
    { re: /^\/extend$/, methods: { POST: () => extendFeed(bucket, feedId, req) } },
    {
      re: /^\/members$/,
      methods: {
        GET: () => listMembers(bucket, feedId, parseWaitMs(url), parseSinceTs(url)),
      },
    },
    {
      re: /^\/members\/([^/]+)$/,
      guard: (m) =>
        validateMemberId(m[1]) ? null : errorResponse("bad-member-id", 400),
      methods: {
        PUT: (m) => putMember(bucket, feedId, m[1], req),
        DELETE: (m) => deleteMember(bucket, feedId, m[1]),
      },
    },
    {
      re: /^\/stream$/,
      methods: { GET: () => streamFeed(env, feedId, url) },
    },
    {
      re: /^\/slots$/,
      methods: {
        GET: () => listFeedSlots(bucket, feedId, parseWaitMs(url), parseSinceTs(url)),
      },
    },
    {
      re: /^\/slots\/([^/]+)$/,
      guard: (m) =>
        validateFeedSlotId(m[1]) ? null : errorResponse("bad-slot-id", 400),
      methods: {
        PUT: (m) => putFeedSlot(bucket, feedId, m[1], req, env.FEED_NOTIFIER, ctx),
        GET: (m) => getFeedSlot(bucket, feedId, m[1], parseWaitMs(url)),
      },
    },
  ];

  for (const route of routes) {
    const m = subpath.match(route.re);
    if (m === null) continue;
    const handler = route.methods[req.method];
    if (handler === undefined) {
      return methodNotAllowed([...Object.keys(route.methods), "OPTIONS"].join(", "));
    }
    const blocked = route.guard?.(m);
    return blocked ?? (await handler(m));
  }
  return errorResponse("not-found", 404);
}

// GET /feeds/:id/stream — SSE push of new-slot notifications (Durable Object).
async function streamFeed(env: Env, feedId: string, url: URL): Promise<Response> {
  const stub = env.FEED_NOTIFIER.get(env.FEED_NOTIFIER.idFromName(feedId));
  const since = url.searchParams.get("since") ?? "0";
  const res = await stub.fetch(
    `https://feed-notifier/connect?feed=${encodeURIComponent(feedId)}` +
      `&since=${encodeURIComponent(since)}`,
  );
  if (res.status !== 200 || res.body === null) {
    return new Response(res.body, { status: res.status });
  }
  // Pipe the DO stream through a local TransformStream so a client cancel is
  // absorbed here instead of surfacing as an unhandled rejection from the DO
  // proxy.
  const { readable, writable } = new TransformStream();
  res.body.pipeTo(writable).catch(() => undefined);
  return new Response(readable, {
    status: 200,
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
    },
  });
}

function methodNotAllowed(allow: string): Response {
  const res = errorResponse("method-not-allowed", 405);
  res.headers.set("Allow", allow);
  return res;
}

async function route(
  req: Request,
  env: Env,
  ctx: ExecutionContext,
  url: URL,
  path: string,
): Promise<Response> {
  if (req.method === "GET" && path === "/health") {
    return new Response("ok", {
      status: 200,
      headers: { "Content-Type": "text/plain" },
    });
  }

  if (!env.NOMNOM_HMAC_SECRET) {
    return errorResponse("relay-misconfigured", 500);
  }

  // POST /feeds — HMAC required (gates feed minting to your relay's users)
  if (req.method === "POST" && path === "/feeds") {
    const auth = await verifyHmac(req, env.NOMNOM_HMAC_SECRET);
    if (!auth.ok) {
      return errorResponse(auth.reason, auth.status);
    }
    return await mintFeed(env.BUCKET, req);
  }

  // /feeds/:id/* — feed-key signature required
  const feedMatch = path.match(/^\/feeds\/([^/]+)(\/.*)?$/);
  if (feedMatch !== null) {
    const feedId = feedMatch[1];
    const subpath = feedMatch[2] ?? "";
    if (!validateFeedId(feedId)) {
      return errorResponse("bad-feed-id", 403);
    }
    const auth = await verifyFeedKey(req, feedId);
    if (!auth.ok) {
      return errorResponse(auth.reason, auth.status);
    }
    return await routeFeed(env, ctx, feedId, subpath, req, url);
  }

  // Legacy: /slots/:slot_id — relay HMAC required
  const slotMatch = path.match(/^\/slots\/([^/]+)$/);
  if (slotMatch !== null) {
    const auth = await verifyHmac(req, env.NOMNOM_HMAC_SECRET);
    if (!auth.ok) {
      return errorResponse(auth.reason, auth.status);
    }
    const slotId = slotMatch[1];
    if (!validateSlotId(slotId)) {
      return errorResponse("bad-slot-id", 403);
    }
    if (req.method === "PUT") return await putSlot(env.BUCKET, slotId, req);
    if (req.method === "GET") {
      return await getSlot(env.BUCKET, slotId, parseWaitMs(url));
    }
    if (req.method === "DELETE") return await deleteSlot(env.BUCKET, slotId);
    return methodNotAllowed("GET, PUT, DELETE, OPTIONS");
  }

  return errorResponse("not-found", 404);
}

export default {
  async fetch(
    req: Request,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<Response> {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(req) });
    }
    return withCors(await route(req, env, ctx, url, url.pathname), req);
  },
};
