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

interface Env {
  BUCKET: R2Bucket;
  NOMNOM_HMAC_SECRET: string;
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
  for (const [k, v] of Object.entries(corsHeaders(req))) res.headers.set(k, v);
  return res;
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

async function routeFeed(
  bucket: R2Bucket,
  feedId: string,
  subpath: string,
  req: Request,
  url: URL,
): Promise<Response> {
  // /feeds/:id (no subpath) — DELETE = close
  if (subpath === "" || subpath === "/") {
    if (req.method === "DELETE") return await closeFeed(bucket, feedId);
    return methodNotAllowed("DELETE, OPTIONS");
  }

  // /feeds/:id/meta
  if (subpath === "/meta") {
    if (req.method === "GET") return await getFeedMeta(bucket, feedId);
    return methodNotAllowed("GET, OPTIONS");
  }

  // /feeds/:id/extend
  if (subpath === "/extend") {
    if (req.method === "POST") return await extendFeed(bucket, feedId, req);
    return methodNotAllowed("POST, OPTIONS");
  }

  // /feeds/:id/members (list / long-poll)
  if (subpath === "/members") {
    if (req.method === "GET") {
      return await listMembers(
        bucket,
        feedId,
        parseWaitMs(url),
        parseSinceTs(url),
      );
    }
    return methodNotAllowed("GET, OPTIONS");
  }

  // /feeds/:id/members/:mid
  const memberMatch = subpath.match(/^\/members\/([^/]+)$/);
  if (memberMatch) {
    const memberId = memberMatch[1];
    if (!validateMemberId(memberId)) {
      return errorResponse("bad-member-id", 403);
    }
    if (req.method === "PUT") {
      return await putMember(bucket, feedId, memberId, req);
    }
    if (req.method === "DELETE") {
      return await deleteMember(bucket, feedId, memberId);
    }
    return methodNotAllowed("PUT, DELETE, OPTIONS");
  }

  // /feeds/:id/slots (list / long-poll)
  if (subpath === "/slots") {
    if (req.method === "GET") {
      return await listFeedSlots(
        bucket,
        feedId,
        parseWaitMs(url),
        parseSinceTs(url),
      );
    }
    return methodNotAllowed("GET, OPTIONS");
  }

  // /feeds/:id/slots/:slot_id
  const slotMatch = subpath.match(/^\/slots\/([^/]+)$/);
  if (slotMatch) {
    const slotId = slotMatch[1];
    if (!validateFeedSlotId(slotId)) {
      return errorResponse("bad-slot-id", 403);
    }
    if (req.method === "PUT") {
      return await putFeedSlot(bucket, feedId, slotId, req);
    }
    if (req.method === "GET") {
      return await getFeedSlot(bucket, feedId, slotId, parseWaitMs(url));
    }
    return methodNotAllowed("PUT, GET, OPTIONS");
  }

  return errorResponse("not-found", 404);
}

function methodNotAllowed(allow: string): Response {
  const res = errorResponse("method-not-allowed", 405);
  res.headers.set("Allow", allow);
  return res;
}

async function route(
  req: Request,
  env: Env,
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
    return await routeFeed(env.BUCKET, feedId, subpath, req, url);
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
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(req) });
    }
    return withCors(await route(req, env, url, url.pathname), req);
  },
};
