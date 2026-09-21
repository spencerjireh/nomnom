// Entry point: fetch() handler + route dispatch.
//
// Routes:
//   GET    /health                                   public; no auth
//   GET    /auth                                     relay HMAC (auth probe, 204)
//   POST   /feeds                                    relay HMAC
//   DELETE /feeds/:id                                feed-key sig
//   GET    /feeds/:id/meta                           feed-key sig
//   GET    /feeds/:id/members?wait=&since=           feed-key sig (long-poll on new joins)
//   PUT    /feeds/:id/members/:mid                   feed-key sig
//   DELETE /feeds/:id/members/:mid                   feed-key sig
//   GET    /feeds/:id/slots?wait=&since=             feed-key sig (long-poll on new posts; since = seq)
//   PUT    /feeds/:id/slots/:slot_id                 feed-key sig (body streams through the DO into R2)
//   GET    /feeds/:id/slots/:slot_id                 feed-key sig (read straight from R2)
//   DELETE /feeds/:id/slots/:slot_id                 feed-key sig
//   GET    /feeds/:id/ws?since=&auth=                feed-key sig (WebSocket; auth may ride the query)
//
// The Worker authenticates, validates ids and bodies, and hands everything
// feed-scoped to the feed's Durable Object (src/feed.ts). Downloads are the
// one exception: they read R2 directly so ciphertext never crosses the DO.
//
// CORS: the browser client (nomnom-web) sends a custom Authorization header value,
// which is NOT CORS-safelisted, so every cross-origin request is preflighted.

import { verifyHmac } from "./auth";
import { verifyFeedKey } from "./feed-auth";
import { Feed } from "./feed";
import {
  MAX_MEMBER_CARD_BYTES,
  slotKey,
  type DoResult,
  type MemberCard,
} from "./feed-types";
import {
  MAX_BODY_BYTES,
  errorResponse,
  jsonResponse,
  parseSince,
  parseWaitMs,
  rejectBody,
} from "./http";
import {
  byteLength,
  generateFeedId,
  isValidCard,
  validateFeedId,
  validateMemberId,
  validateSlotId,
} from "./validate";

export { Feed };

interface Env {
  BUCKET: R2Bucket;
  NOMNOM_HMAC_SECRET: string;
  FEED: DurableObjectNamespace<Feed>;
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
  // A 101 carries the WebSocket on the Response object itself; rebuilding it
  // would drop the socket. Browsers don't apply CORS to upgrades anyway.
  if (res.status === 101) return res;
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

// Fold a DO RPC result into a Response: the value as JSON, or an empty body
// for 204.
function fromResult<T>(r: DoResult<T>, okStatus: number): Response {
  if (!r.ok) return errorResponse(r.error, r.status);
  if (okStatus === 204) return new Response(null, { status: 204 });
  return jsonResponse(r.value, okStatus);
}

function parseJson(text: string): unknown | undefined {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

// Validate a member card and produce the JSON the DO stores verbatim, or an
// error Response. Shared by mint (card nested in the body) and PUT member
// (card IS the body).
function checkCard(
  card: unknown,
  expectMemberId?: string,
): { card: MemberCard; json: string } | Response {
  if (!isValidCard(card, expectMemberId)) {
    return errorResponse("bad-member-card", 400);
  }
  const json = JSON.stringify(card);
  if (byteLength(json) > MAX_MEMBER_CARD_BYTES) {
    return errorResponse("member-card-too-large", 413);
  }
  return { card, json };
}

// ---------- POST /feeds ----------

async function mintFeed(env: Env, req: Request): Promise<Response> {
  const body = parseJson(await req.text()) as { member_card?: unknown } | undefined;
  if (body === undefined) return errorResponse("bad-json", 400);
  const parsed = checkCard(body?.member_card);
  if (parsed instanceof Response) return parsed;
  // 72-bit ids never collide in practice; `feed-exists` surfaces as a 409
  // like any other unexpected DO result.
  const feedId = generateFeedId();
  const stub = env.FEED.get(env.FEED.idFromName(feedId));
  const r = await stub.create(feedId, parsed.card, parsed.json);
  if (!r.ok) return errorResponse(r.error, r.status);
  return jsonResponse({ feed_id: feedId, created_at: r.value.created_at }, 201);
}

// ---------- /feeds/:id/* ----------

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
  feedId: string,
  subpath: string,
  req: Request,
  url: URL,
): Promise<Response> {
  const stub = env.FEED.get(env.FEED.idFromName(feedId));
  const since = parseSince(url.searchParams.get("since"));
  const wait = parseWaitMs(url.searchParams.get("wait"));

  const routes: FeedRoute[] = [
    {
      re: /^\/?$/,
      methods: {
        DELETE: async () => fromResult(await stub.purge(), 204),
      },
    },
    {
      re: /^\/meta$/,
      methods: {
        GET: async () => fromResult(await stub.meta(), 200),
      },
    },
    {
      re: /^\/members$/,
      methods: {
        GET: async () => fromResult(await stub.listMembers(since, wait), 200),
      },
    },
    {
      re: /^\/members\/([^/]+)$/,
      guard: (m) =>
        validateMemberId(m[1]) ? null : errorResponse("bad-member-id", 400),
      methods: {
        PUT: async (m) => {
          const body = parseJson(await req.text());
          if (body === undefined) return errorResponse("bad-json", 400);
          const parsed = checkCard(body, m[1]);
          if (parsed instanceof Response) return parsed;
          return fromResult(await stub.putMember(parsed.card, parsed.json), 204);
        },
        DELETE: async (m) => fromResult(await stub.deleteMember(m[1]), 204),
      },
    },
    {
      re: /^\/slots$/,
      methods: {
        GET: async () => fromResult(await stub.listSlots(since, wait), 200),
      },
    },
    {
      re: /^\/slots\/([^/]+)$/,
      guard: (m) =>
        validateSlotId(m[1]) ? null : errorResponse("bad-slot-id", 400),
      methods: {
        PUT: async (m) => {
          const rejected = rejectBody(req, MAX_BODY_BYTES);
          if (rejected) return rejected;
          // Forward the ORIGINAL request with only the URL rewritten: its body
          // carries the client's known Content-Length, which R2 needs. A
          // Request built from a bare ReadableStream would go out chunked.
          return stub.fetch(new Request(`https://feed/slots/${m[1]}`, req));
        },
        GET: async (m) => {
          // Straight from R2: blob existence is the liveness proof (only a
          // created feed produces blobs; purge/alarm/failed-insert remove
          // them), and ciphertext never has to cross the DO.
          const obj = await env.BUCKET.get(slotKey(feedId, m[1]));
          if (obj === null) return errorResponse("not-found", 404);
          return new Response(obj.body, {
            status: 200,
            headers: { "Content-Type": "application/octet-stream" },
          });
        },
        DELETE: async (m) => fromResult(await stub.deletePost(m[1]), 204),
      },
    },
    {
      re: /^\/ws$/,
      methods: {
        GET: async () => {
          if (req.headers.get("Upgrade") !== "websocket") {
            return errorResponse("expected-websocket", 426);
          }
          const target = new URL("https://feed/ws");
          target.searchParams.set("since", String(since));
          return stub.fetch(new Request(target, req));
        },
      },
    },
  ];

  for (const route of routes) {
    const m = subpath.match(route.re);
    if (m === null) continue;
    const handler = route.methods[req.method];
    if (handler === undefined) {
      return methodNotAllowed(
        [...Object.keys(route.methods), "OPTIONS"].join(", "),
      );
    }
    const blocked = route.guard?.(m);
    return blocked ?? (await handler(m));
  }
  return errorResponse("not-found", 404);
}

function methodNotAllowed(allow: string): Response {
  const res = errorResponse("method-not-allowed", 405);
  res.headers.set("Allow", allow);
  return res;
}

async function route(req: Request, env: Env, url: URL): Promise<Response> {
  const path = url.pathname;
  if (path === "/health") {
    if (req.method !== "GET") return methodNotAllowed("GET, OPTIONS");
    return new Response("ok", {
      status: 200,
      headers: { "Content-Type": "text/plain" },
    });
  }

  if (!env.NOMNOM_HMAC_SECRET) {
    return errorResponse("relay-misconfigured", 500);
  }

  // GET /auth — proves the caller holds the relay secret. Used by clients to
  // verify a pasted secret before they try to mint.
  if (path === "/auth") {
    if (req.method !== "GET") return methodNotAllowed("GET, OPTIONS");
    const auth = await verifyHmac(req, env.NOMNOM_HMAC_SECRET);
    if (!auth.ok) return errorResponse(auth.reason, auth.status);
    return new Response(null, { status: 204 });
  }

  // POST /feeds — HMAC required (gates feed minting to your relay's users)
  if (path === "/feeds") {
    if (req.method !== "POST") return methodNotAllowed("POST, OPTIONS");
    const auth = await verifyHmac(req, env.NOMNOM_HMAC_SECRET);
    if (!auth.ok) return errorResponse(auth.reason, auth.status);
    return await mintFeed(env, req);
  }

  // /feeds/:id/* — feed-key signature required
  const feedMatch = path.match(/^\/feeds\/([^/]+)(\/.*)?$/);
  if (feedMatch !== null) {
    const feedId = feedMatch[1];
    const subpath = feedMatch[2] ?? "";
    if (!validateFeedId(feedId)) {
      return errorResponse("bad-feed-id", 400);
    }
    const auth = await verifyFeedKey(req, feedId);
    if (!auth.ok) return errorResponse(auth.reason, auth.status);
    // Every DO interaction shares one fault contract: a rejected stub call
    // (object evicted mid-call, runtime fault) is 502, so clients can tell
    // "retry" from "your request is wrong".
    try {
      return await routeFeed(env, feedId, subpath, req, url);
    } catch {
      return errorResponse("feed-unavailable", 502);
    }
  }

  return errorResponse("not-found", 404);
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(req) });
    }
    return withCors(await route(req, env, url), req);
  },
};
