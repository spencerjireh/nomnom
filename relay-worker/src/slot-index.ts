// Shared slot-index scan used by the /feeds/:id/slots long-poll (first pass) and
// the SSE notifier's backlog replay. Both list `feeds/<id>/slots/`, head each
// object for its `created_at`, keep those newer than a cursor, and sort ascending.

export const LIST_BATCH_SIZE = 1000;
// Cap on concurrent R2 head sub-requests. Workers free tier allows 50
// subrequests per invocation, paid 1000; 16 keeps us comfortable even with the
// long-poll's surrounding reads.
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

// One-shot scan of all slots written after `sinceTs`, sorted ascending. Heads
// are issued in chunks of SLOT_HEAD_CONCURRENCY to respect the subrequest cap.
export async function readSlotsSince(
  bucket: R2Bucket,
  feedId: string,
  sinceTs: number,
): Promise<SlotIndex[]> {
  const prefix = `feeds/${feedId}/slots/`;
  const list = await bucket.list({ prefix, limit: LIST_BATCH_SIZE });
  const fresh: SlotIndex[] = [];
  for (let i = 0; i < list.objects.length; i += SLOT_HEAD_CONCURRENCY) {
    const slice = list.objects.slice(i, i + SLOT_HEAD_CONCURRENCY);
    const heads = await Promise.all(slice.map((o) => bucket.head(o.key)));
    for (let j = 0; j < slice.length; j++) {
      const createdAt = parseTs(heads[j]?.customMetadata?.created_at);
      if (createdAt === null || createdAt <= sinceTs) continue;
      fresh.push({ slot_id: slice[j].key.slice(prefix.length), created_at: createdAt });
    }
  }
  fresh.sort((a, b) => a.created_at - b.created_at);
  return fresh;
}
