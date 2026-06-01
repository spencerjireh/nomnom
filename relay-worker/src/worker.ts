// Entry point: fetch() handler + route dispatch.
//
// Routes (HMAC required unless noted):
//   PUT    /slots/:slot_id            store a payload (≤256 MB) for 5 minutes
//   GET    /slots/:slot_id?wait=<ms>  fetch and delete (long-poll, max 30s)
//   DELETE /slots/:slot_id            erase a slot (cleanup on cancel)
//   GET    /health                    plain "ok" (no HMAC) — connectivity check
//   OPTIONS *                         CORS preflight (no HMAC)
//
// CORS: the browser client (nomnom-web) sends a custom Authorization header value,
// which is NOT CORS-safelisted, so every cross-origin request is preflighted. We
// allowlist the prod web origin + localhost dev, echo the Origin, and wrap every
// response so long-poll 200s and error bodies are readable by the browser.

import { verifyHmac } from "./auth";
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
    "Access-Control-Allow-Methods": "GET, PUT, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}

function withCors(res: Response, req: Request): Response {
  const h = corsHeaders(req);
  for (const k in h) res.headers.set(k, h[k]);
  return res;
}

async function route(req: Request, env: Env, url: URL, path: string): Promise<Response> {
  if (req.method === "GET" && path === "/health") {
    return new Response("ok", {
      status: 200,
      headers: { "Content-Type": "text/plain" },
    });
  }

  if (!env.NOMNOM_HMAC_SECRET) {
    return new Response("relay-misconfigured", { status: 500 });
  }

  const auth = await verifyHmac(req, env.NOMNOM_HMAC_SECRET);
  if (!auth.ok) {
    return new Response(auth.reason, { status: auth.status });
  }

  const slotMatch = path.match(/^\/slots\/([^/]+)$/);
  if (slotMatch === null) {
    return new Response("not-found", { status: 404 });
  }
  const slotId = slotMatch[1];
  if (!validateSlotId(slotId)) {
    return new Response("bad-slot-id", { status: 403 });
  }

  if (req.method === "PUT") {
    return await putSlot(env.BUCKET, slotId, req);
  }
  if (req.method === "GET") {
    const waitStr = url.searchParams.get("wait") ?? "0";
    const waitMs = Number.parseInt(waitStr, 10);
    return await getSlot(
      env.BUCKET,
      slotId,
      Number.isFinite(waitMs) && waitMs > 0 ? waitMs : 0,
    );
  }
  if (req.method === "DELETE") {
    return await deleteSlot(env.BUCKET, slotId);
  }
  return new Response("method-not-allowed", {
    status: 405,
    headers: { Allow: "GET, PUT, DELETE, OPTIONS" },
  });
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    // Preflight: answer before HMAC (the browser sends no Authorization on it).
    if (req.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders(req) });
    }
    return withCors(await route(req, env, url, url.pathname), req);
  },
};
