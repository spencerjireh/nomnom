// Long-poll helper. Holds the request open for up to budgetMs (capped at
// 30s), polling R2 every POLL_INTERVAL_MS for the slot to appear.
//
// setTimeout idle does not count against Workers CPU time, so a 30s
// long-poll fits comfortably under the free tier wall-clock cap.

import { pollDeadline, sleep } from "./http";

const POLL_INTERVAL_MS = 500;

export async function pollSlot(
  bucket: R2Bucket,
  key: string,
  budgetMs: number,
): Promise<R2ObjectBody | null> {
  const deadline = pollDeadline(budgetMs);
  while (true) {
    const obj = await bucket.get(key);
    if (obj) return obj;
    const remaining = deadline - Date.now();
    if (remaining <= 0) return null;
    await sleep(Math.min(POLL_INTERVAL_MS, remaining));
  }
}
