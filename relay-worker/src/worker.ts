// Entry point: fetch() handler + route dispatch.
//
// Routes (HMAC required unless noted):
//   PUT    /slots/:slot_id            store a payload (≤256 MB) for 5 minutes
//   GET    /slots/:slot_id?wait=<ms>  fetch and delete (long-poll, max 30s)
//   DELETE /slots/:slot_id            erase a slot (cleanup on cancel)
//   GET    /health                    plain "ok" (no HMAC) — connectivity check

import { verifyHmac } from "./auth";
import { deleteSlot, getSlot, putSlot, validateSlotId } from "./slots";

interface Env {
  BUCKET: R2Bucket;
  NOMNOM_HMAC_SECRET: string;
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    const url = new URL(req.url);
    const path = url.pathname;

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
      headers: { Allow: "PUT, GET, DELETE" },
    });
  },
};
