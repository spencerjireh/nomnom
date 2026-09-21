import { SELF } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import {
  BASE,
  feedWsUrl,
  mintFeed,
  openFeedWs,
  putSlot,
  randomBase64,
  randomMemberId,
  signedFeedRequest,
} from "./helpers";

describe("GET /feeds/:id/ws (WebSocket push)", () => {
  it("upgrades to a WebSocket (101)", async () => {
    const m = await mintFeed(SELF);
    const conn = await openFeedWs(await feedWsUrl(m.feed_id));
    try {
      expect(conn.status).toBe(101);
      expect(conn.ws).not.toBeNull();
    } finally {
      conn.close();
    }
  });

  it("426 without an Upgrade header", async () => {
    const m = await mintFeed(SELF);
    const res = await SELF.fetch(await feedWsUrl(m.feed_id));
    expect(res.status).toBe(426);
  });

  it("404 on an unknown feed", async () => {
    const fakeId = randomBase64(9);
    const res = await SELF.fetch(await feedWsUrl(fakeId), {
      headers: { Upgrade: "websocket" },
    });
    expect(res.status).toBe(404);
  });

  it("replays posts with seq > since on connect, in order", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "back-1");
    await putSlot(m.feed_id, "back-2");
    const conn = await openFeedWs(await feedWsUrl(m.feed_id, 0));
    try {
      const frames = await conn.next(2);
      expect(frames.map((f) => f.type)).toEqual(["post", "post"]);
      expect(frames.map((f) => f.slot_id)).toEqual(["back-1", "back-2"]);
      expect(frames.map((f) => f.seq)).toEqual([1, 2]);
    } finally {
      conn.close();
    }
  });

  it("respects the since cursor", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "old");
    const conn = await openFeedWs(await feedWsUrl(m.feed_id, 1));
    try {
      const none = await conn.next(1, 800);
      expect(none).toEqual([]);
      await putSlot(m.feed_id, "new");
      const frames = await conn.next(1);
      expect(frames.map((f) => f.slot_id)).toEqual(["new"]);
      expect(frames[0].seq).toBe(2);
    } finally {
      conn.close();
    }
  });

  it("pushes a post made after connect (live)", async () => {
    const m = await mintFeed(SELF);
    const conn = await openFeedWs(await feedWsUrl(m.feed_id));
    try {
      await putSlot(m.feed_id, "live-1");
      const frames = await conn.next(1);
      expect(frames[0]).toMatchObject({ type: "post", slot_id: "live-1", seq: 1 });
      expect(typeof frames[0].created_at).toBe("number");
    } finally {
      conn.close();
    }
  });

  it("emits member join and leave frames", async () => {
    const m = await mintFeed(SELF);
    const conn = await openFeedWs(await feedWsUrl(m.feed_id));
    try {
      const mid = randomMemberId();
      await SELF.fetch(
        await signedFeedRequest("PUT", `/feeds/${m.feed_id}/members/${mid}`, m.feed_id, {
          body: JSON.stringify({
            member_id: mid,
            identity_pubkey: randomBase64(32),
            name: "joiner",
          }),
        }),
      );
      const [join] = await conn.next(1);
      expect(join).toMatchObject({
        type: "member",
        action: "join",
        member: { member_id: mid, name: "joiner" },
      });
      await SELF.fetch(
        await signedFeedRequest("DELETE", `/feeds/${m.feed_id}/members/${mid}`, m.feed_id),
      );
      const frames = await conn.next(2);
      expect(frames[1]).toMatchObject({
        type: "member",
        action: "leave",
        member: { member_id: mid },
      });
    } finally {
      conn.close();
    }
  });

  it("emits a deleted frame when a post is hard-deleted", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "doomed");
    const conn = await openFeedWs(await feedWsUrl(m.feed_id, 1));
    try {
      await SELF.fetch(
        await signedFeedRequest("DELETE", `/feeds/${m.feed_id}/slots/doomed`, m.feed_id),
      );
      const [del] = await conn.next(1);
      expect(del).toEqual({ type: "deleted", slot_id: "doomed" });
    } finally {
      conn.close();
    }
  });

  it("two sockets on one feed both receive the frame", async () => {
    const m = await mintFeed(SELF);
    const a = await openFeedWs(await feedWsUrl(m.feed_id));
    const b = await openFeedWs(await feedWsUrl(m.feed_id));
    try {
      await putSlot(m.feed_id, "fanout");
      const [fa] = await a.next(1);
      const [fb] = await b.next(1);
      expect(fa.slot_id).toBe("fanout");
      expect(fb.slot_id).toBe("fanout");
    } finally {
      a.close();
      b.close();
    }
  });

  it("accepts query-param auth but rejects a bad MAC", async () => {
    const m = await mintFeed(SELF);
    const res = await SELF.fetch(
      await feedWsUrl(m.feed_id, 0, { macOverride: "00".repeat(32) }),
      { headers: { Upgrade: "websocket" } },
    );
    expect(res.status).toBe(401);
  });

  it("rejects an out-of-window (clock-skew) timestamp", async () => {
    const m = await mintFeed(SELF);
    const res = await SELF.fetch(
      await feedWsUrl(m.feed_id, 0, {
        tsOverride: Math.floor(Date.now() / 1000) - 600,
      }),
      { headers: { Upgrade: "websocket" } },
    );
    expect(res.status).toBe(401);
    expect(await res.text()).toContain("clock-skew");
  });

  it("closes sockets when the feed is purged", async () => {
    const m = await mintFeed(SELF);
    const conn = await openFeedWs(await feedWsUrl(m.feed_id));
    const closed = new Promise<number>((resolve) => {
      conn.ws!.addEventListener("close", (ev) => resolve(ev.code));
    });
    await SELF.fetch(await signedFeedRequest("DELETE", `/feeds/${m.feed_id}`, m.feed_id));
    expect(await closed).toBe(1000);
    void BASE;
  });
});
