// Expiry/purge lifecycle. No time mocking needed: tests rewrite the meta
// object directly via the test R2 binding with an `expires_at` in the past,
// then assert every read/write path 410s and the prefix gets purged.
import { env, SELF } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { touchFeedMeta } from "../src/feeds";
import {
  mintFeed,
  randomBase64,
  randomMemberId,
  signedFeedRequest,
} from "./helpers";

const bucket = (env as unknown as { BUCKET: R2Bucket }).BUCKET;

// Mirror the mint-time meta shape (feeds.ts) with expires_at in the past.
async function expireFeed(feedId: string, createdAt: number): Promise<void> {
  const past = Math.floor(Date.now() / 1000) - 10;
  await bucket.put(
    `feeds/${feedId}/meta`,
    JSON.stringify({ created_at: createdAt, expires_at: past }),
    {
      customMetadata: { expires_at: String(past) },
      httpMetadata: { contentType: "application/json" },
    },
  );
}

describe("feed expiry", () => {
  it("GET /meta on an expired feed returns 410 and purges the prefix", async () => {
    const m = await mintFeed(SELF);
    // Populate the prefix beyond meta: one slot + one extra member card.
    const putSlot = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/slots/exp-slot`,
      m.feed_id,
      { body: "ciphertext" },
    );
    expect((await SELF.fetch(putSlot)).status).toBe(204);
    const memberId = randomMemberId();
    const putMember = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/members/${memberId}`,
      m.feed_id,
      {
        body: JSON.stringify({
          member_id: memberId,
          identity_pubkey: randomBase64(32),
          name: "second-device",
        }),
      },
    );
    expect((await SELF.fetch(putMember)).status).toBe(204);

    await expireFeed(m.feed_id, m.created_at);

    const metaReq = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/meta`,
      m.feed_id,
    );
    const res = await SELF.fetch(metaReq);
    expect(res.status).toBe(410);
    expect(((await res.json()) as { error: string }).error).toBe("feed-expired");

    // The 410 is sent only after the purge completes — prefix must be empty.
    const list = await bucket.list({ prefix: `feeds/${m.feed_id}/` });
    expect(list.objects.length).toBe(0);

    // A second read finds nothing left.
    const again = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/meta`,
      m.feed_id,
    );
    expect((await SELF.fetch(again)).status).toBe(404);
  });

  // Each gate gets its own expired feed: the first 410 purges the prefix,
  // so a shared feed would answer 404 (feed-not-found) to later checks.
  it("slot write gates on feed liveness", async () => {
    const m = await mintFeed(SELF);
    await expireFeed(m.feed_id, m.created_at);
    const putReq = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/slots/post-expiry`,
      m.feed_id,
      { body: "ciphertext" },
    );
    expect((await SELF.fetch(putReq)).status).toBe(410);
  });

  it("slot read gates on feed liveness", async () => {
    const m = await mintFeed(SELF);
    const putLive = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/slots/pre-expiry`,
      m.feed_id,
      { body: "ciphertext" },
    );
    expect((await SELF.fetch(putLive)).status).toBe(204);
    await expireFeed(m.feed_id, m.created_at);
    const getReq = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/slots/pre-expiry`,
      m.feed_id,
    );
    expect((await SELF.fetch(getReq)).status).toBe(410);
  });

  it("slot list gates on feed liveness", async () => {
    const m = await mintFeed(SELF);
    await expireFeed(m.feed_id, m.created_at);
    const listReq = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/slots?since=0&wait_ms=0`,
      m.feed_id,
    );
    expect((await SELF.fetch(listReq)).status).toBe(410);
  });
});

describe("touchFeedMeta", () => {
  it("does not clobber a meta written between its get and put", async () => {
    const m = await mintFeed(SELF);
    const key = `feeds/${m.feed_id}/meta`;
    const bumped = m.expires_at + 9999;
    const newer = JSON.stringify({
      created_at: m.created_at,
      expires_at: bumped,
    });
    // Wrapper bucket: an "extendFeed" lands between the touch's get and put,
    // bumping the etag — so the touch's conditional put must be dropped.
    const racing = {
      get: (k: string) => bucket.get(k),
      put: async (k: string, v: string, o: R2PutOptions) => {
        await bucket.put(key, newer, {
          customMetadata: { expires_at: String(bumped) },
          httpMetadata: { contentType: "application/json" },
        });
        return bucket.put(k, v, o);
      },
    } as unknown as R2Bucket;
    await touchFeedMeta(racing, m.feed_id);
    const final = await bucket.get(key);
    expect(JSON.parse(await final!.text()).expires_at).toBe(bumped);
    expect(final!.customMetadata?.expires_at).toBe(String(bumped));
  });
});
