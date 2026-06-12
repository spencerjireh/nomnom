// Slot-index helpers. `headCreatedAts` (the chunked created_at head scan) is
// shared by the /feeds/:id/slots long-poll and the SSE notifier's backlog
// replay; `readSlotsSince` (one-shot scan, cursor filter, ascending sort) is
// used only by the notifier.

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

// Head `objs` in chunks of SLOT_HEAD_CONCURRENCY (subrequest budget),
// returning key -> created_at for objects with a parseable created_at.
export async function headCreatedAts(
  bucket: R2Bucket,
  objs: { key: string }[],
): Promise<Map<string, number>> {
  const out = new Map<string, number>();
  for (let i = 0; i < objs.length; i += SLOT_HEAD_CONCURRENCY) {
    const slice = objs.slice(i, i + SLOT_HEAD_CONCURRENCY);
    const heads = await Promise.all(slice.map((o) => bucket.head(o.key)));
    for (let j = 0; j < slice.length; j++) {
      const ts = parseTs(heads[j]?.customMetadata?.created_at);
      if (ts !== null) out.set(slice[j].key, ts);
    }
  }
  return out;
}

// One-shot scan of all slots written after `sinceTs`, sorted ascending.
export async function readSlotsSince(
  bucket: R2Bucket,
  feedId: string,
  sinceTs: number,
): Promise<SlotIndex[]> {
  const prefix = `feeds/${feedId}/slots/`;
  const list = await bucket.list({ prefix, limit: LIST_BATCH_SIZE });
  const createdAts = await headCreatedAts(bucket, list.objects);
  const fresh: SlotIndex[] = [];
  for (const [key, createdAt] of createdAts) {
    if (createdAt <= sinceTs) continue;
    fresh.push({ slot_id: key.slice(prefix.length), created_at: createdAt });
  }
  fresh.sort((a, b) => a.created_at - b.created_at);
  return fresh;
}
