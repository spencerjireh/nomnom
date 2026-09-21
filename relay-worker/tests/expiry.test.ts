// Retention is the DO alarm's job: posts age out 30 days after creation, and a
// feed with no posts left and 30 days of inactivity is purged entirely.

import { env, runDurableObjectAlarm, runInDurableObject, SELF } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { feedStub, mintFeed, putSlot, signedFeedRequest } from "./helpers";

const DAY = 86_400;

async function listSlotIds(feedId: string): Promise<string[]> {
  const res = await SELF.fetch(
    await signedFeedRequest("GET", `/feeds/${feedId}/slots?since=0`, feedId),
  );
  const data = (await res.json()) as { slots: { slot_id: string }[] };
  return data.slots.map((s) => s.slot_id);
}

describe("alarm: post retention", () => {
  it("is scheduled at mint, roughly a day out", async () => {
    const m = await mintFeed(SELF);
    await runInDurableObject(feedStub(m.feed_id), async (_i, state) => {
      const at = await state.storage.getAlarm();
      expect(at).not.toBeNull();
      expect(at! - Date.now()).toBeGreaterThan(23 * 3600 * 1000);
      expect(at! - Date.now()).toBeLessThanOrEqual(24 * 3600 * 1000);
    });
  });

  it("deletes a 31-day-old post and its blob but keeps the feed", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "old");
    await putSlot(m.feed_id, "new");
    const oldKey = `feeds/${m.feed_id}/slots/old`;
    const newKey = `feeds/${m.feed_id}/slots/new`;
    const stub = feedStub(m.feed_id);
    await runInDurableObject(stub, async (_i, state) => {
      state.storage.sql.exec(
        "UPDATE posts SET created_at = ? WHERE slot_id = 'old'",
        Math.floor(Date.now() / 1000) - 31 * DAY,
      );
    });

    expect(await runDurableObjectAlarm(stub)).toBe(true);

    expect(await env.BUCKET.head(oldKey)).toBeNull();
    expect(await env.BUCKET.head(newKey)).not.toBeNull();
    expect(await listSlotIds(m.feed_id)).toEqual(["new"]);
    await runInDurableObject(stub, async (_i, state) => {
      expect(await state.storage.getAlarm()).not.toBeNull(); // rescheduled
    });
  });

  it("keeps a feed that is idle but still has fresh posts", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "fresh");
    const stub = feedStub(m.feed_id);
    await runInDurableObject(stub, async (_i, state) => {
      state.storage.sql.exec(
        "UPDATE feed SET last_used_at = ?",
        Math.floor(Date.now() / 1000) - 31 * DAY,
      );
    });
    expect(await runDurableObjectAlarm(stub)).toBe(true);
    // The in-memory lastUsedAt cache still says "just now"; the alarm reads
    // the cache, so this exercises the "posts remain" branch regardless.
    const meta = await SELF.fetch(
      await signedFeedRequest("GET", `/feeds/${m.feed_id}/meta`, m.feed_id),
    );
    expect(meta.status).toBe(200);
    expect(await listSlotIds(m.feed_id)).toEqual(["fresh"]);
  });

  it("purges an idle feed with no posts, including its DO storage", async () => {
    const m = await mintFeed(SELF);
    const stub = feedStub(m.feed_id);
    // Backdate both the row and the instance's cached copy (the alarm reads the
    // cache, which the constructor seeds from the row on a cold start).
    await runInDurableObject(stub, async (instance, state) => {
      const stale = Math.floor(Date.now() / 1000) - 31 * DAY;
      state.storage.sql.exec("UPDATE feed SET last_used_at = ?", stale);
      (instance as unknown as { lastUsedAt: number }).lastUsedAt = stale;
    });

    expect(await runDurableObjectAlarm(stub)).toBe(true);

    const meta = await SELF.fetch(
      await signedFeedRequest("GET", `/feeds/${m.feed_id}/meta`, m.feed_id),
    );
    expect(meta.status).toBe(404);
    await runInDurableObject(stub, async (_i, state) => {
      // deleteAll() drops our tables; SQLite's `sqlite_sequence` and the
      // runtime's `_cf_METADATA` are not ours and may linger.
      const tables = state.storage.sql
        .exec(
          "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE '_cf_%'",
        )
        .toArray();
      expect(tables.map((t) => t.name)).toEqual([]);
      expect(await state.storage.getAlarm()).toBeNull();
    });
  });
});
