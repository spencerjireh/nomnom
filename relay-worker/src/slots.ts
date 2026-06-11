// R2 slot read/write/delete with TTL metadata.
//
// Lifecycle:
//   PUT writes `customMetadata.expires_at` (unix seconds).
//   GET checks expires_at; if past, deletes and returns 410 Gone.
//   GET on a live slot returns the body AND deletes (delete-on-read).
//
// Belt-and-suspenders: configure an R2 bucket lifecycle rule to delete
// objects older than 1 day so orphans (sender abandons after PUT, etc.)
// don't accumulate. See relay-worker/README.md for the dashboard step.

import { errorResponse } from "./http";
import { pollSlot } from "./poll";

const SLOT_TTL_SEC = 300; // 5 minutes from PUT to expiry
const MAX_BODY_BYTES = 256 * 1024 * 1024; // 256 MB (free tier caps at 100 MB at the edge)
const SLOT_ID_RE = /^[A-Za-z0-9_-]{1,128}$/;

export function validateSlotId(slotId: string): boolean {
  return SLOT_ID_RE.test(slotId);
}

export async function putSlot(
  bucket: R2Bucket,
  key: string,
  req: Request,
): Promise<Response> {
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
  const expiresAt = Math.floor(Date.now() / 1000) + SLOT_TTL_SEC;
  // Atomic create-if-absent: the conditional put returns null when the slot
  // already exists, so a racing PUT gets 409 instead of clobbering.
  const created = await bucket.put(key, req.body, {
    onlyIf: { etagDoesNotMatch: "*" },
    customMetadata: { expires_at: String(expiresAt) },
  });
  if (created === null) {
    return errorResponse("slot-occupied", 409);
  }
  return new Response(null, { status: 204 });
}

export async function getSlot(
  bucket: R2Bucket,
  key: string,
  waitMs: number,
): Promise<Response> {
  const obj = await pollSlot(bucket, key, waitMs);
  if (obj === null) {
    return errorResponse("not-found", 404);
  }
  const expStr = obj.customMetadata?.expires_at;
  if (expStr !== undefined) {
    const exp = Number.parseInt(expStr, 10);
    if (Number.isFinite(exp) && exp < Math.floor(Date.now() / 1000)) {
      await bucket.delete(key);
      return errorResponse("expired", 410);
    }
  }
  // Delete-on-read. We have to fully read the body first before delete
  // since R2's delete is independent of any open handle to the object.
  // Stream the body directly into the Response and run the delete after
  // the body is finished via ctx.waitUntil from the caller... but slots.ts
  // doesn't have ctx. So we delete eagerly and stream the in-flight body.
  // This is safe: bucket.delete only removes the key from the index; the
  // object body we've already opened is still readable.
  await bucket.delete(key);
  return new Response(obj.body, {
    status: 200,
    headers: { "Content-Type": "application/octet-stream" },
  });
}

export async function deleteSlot(
  bucket: R2Bucket,
  key: string,
): Promise<Response> {
  await bucket.delete(key);
  return new Response(null, { status: 204 });
}
