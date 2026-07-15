// Slot-index helpers. `listSlotCreatedAts` (paginated list with created_at from
// customMetadata) backs both the /feeds/:id/slots long-poll and the SSE
// notifier's backlog replay via `readSlotsSince` (one-shot scan, cursor filter,
// ascending sort).

export const LIST_BATCH_SIZE = 1000;
// Cap on concurrent R2 sub-requests when fetching object bodies (e.g. the member
// roster scan). Workers free tier allows 50 subrequests per invocation, paid
// 1000; 16 keeps us comfortable even with surrounding reads.
export const SLOT_HEAD_CONCURRENCY = 16;

export interface SlotIndex {
  slot_id: string;
  created_at: number;
}

export function parseTs(s: string | undefined): number | null {
  if (s === undefined) return null;
  const n = Number.parseInt(s, 10);
  return Number.isFinite(n) ? n : null;
}

// List EVERY slot under a feed (following the R2 list cursor across pages) and
// return key -> created_at read straight from customMetadata. Passing
// `include: ["customMetadata"]` means created_at comes back on each listed
// object, so we pay only for the list pages (one sub-request per page) instead
// of an O(slot_count) per-slot head scan — which would blow the Workers
// subrequest cap on a busy channel. A single un-paginated list capped at
// LIST_BATCH_SIZE would silently drop every slot past the first page.
export async function listSlotCreatedAts(
  bucket: R2Bucket,
  feedId: string,
): Promise<Map<string, number>> {
  const prefix = `feeds/${feedId}/slots/`;
  const out = new Map<string, number>();
  let cursor: string | undefined;
  do {
    const list = await bucket.list({
      prefix,
      limit: LIST_BATCH_SIZE,
      cursor,
      include: ["customMetadata"],
    });
    for (const o of list.objects) {
      const ts = parseTs(o.customMetadata?.created_at);
      if (ts !== null) out.set(o.key, ts);
    }
    cursor = list.truncated ? list.cursor : undefined;
  } while (cursor !== undefined);
  return out;
}

// One-shot scan of all slots written after `sinceTs`, sorted ascending.
export async function readSlotsSince(
  bucket: R2Bucket,
  feedId: string,
  sinceTs: number,
): Promise<SlotIndex[]> {
  const prefix = `feeds/${feedId}/slots/`;
  const createdAts = await listSlotCreatedAts(bucket, feedId);
  const fresh: SlotIndex[] = [];
  for (const [key, createdAt] of createdAts) {
    if (createdAt <= sinceTs) continue;
    fresh.push({ slot_id: key.slice(prefix.length), created_at: createdAt });
  }
  fresh.sort((a, b) => a.created_at - b.created_at);
  return fresh;
}
