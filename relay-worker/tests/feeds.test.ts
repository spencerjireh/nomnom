import { SELF } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import {
  BASE,
  mintFeed,
  randomBase64,
  randomMemberId,
  signedFeedRequest,
  signedHmacRequest,
} from "./helpers";

describe("health + auth", () => {
  it("GET /health needs no auth", async () => {
    const res = await SELF.fetch(`${BASE}/health`);
    expect(res.status).toBe(200);
    expect(await res.text()).toBe("ok");
  });

  it("rejects unauthenticated POST /feeds", async () => {
    const res = await SELF.fetch(`${BASE}/feeds`, { method: "POST" });
    expect(res.status).toBe(401);
  });

  it("rejects unauthenticated /feeds/:id/meta", async () => {
    const res = await SELF.fetch(`${BASE}/feeds/abcdefghij/meta`);
    expect(res.status).toBe(401);
  });
});

describe("POST /feeds (mint)", () => {
  it("mints a feed and returns id/expiry", async () => {
    const minted = await mintFeed(SELF);
    expect(minted.feed_id).toMatch(/^[A-Za-z0-9_-]{12}$/);
    expect(minted.expires_at).toBeGreaterThan(minted.created_at);
  });

  it("rejects malformed member_card", async () => {
    const req = await signedHmacRequest("POST", "/feeds", {
      body: JSON.stringify({ ttl_seconds: 100, member_card: { name: "x" } }),
    });
    const res = await SELF.fetch(req);
    expect(res.status).toBe(400);
  });

  it("rejects malformed JSON", async () => {
    const req = await signedHmacRequest("POST", "/feeds", {
      body: "not-json{",
    });
    const res = await SELF.fetch(req);
    expect(res.status).toBe(400);
  });

  it("clamps absurdly long TTL", async () => {
    const minted = await mintFeed(SELF, { ttlSeconds: 10 ** 12 });
    expect(minted.expires_at - minted.created_at).toBeLessThanOrEqual(
      90 * 86_400,
    );
  });

  it("clamps absurdly short TTL", async () => {
    const minted = await mintFeed(SELF, { ttlSeconds: 0 });
    expect(minted.expires_at - minted.created_at).toBeGreaterThanOrEqual(60);
  });
});

describe("GET /feeds/:id/meta", () => {
  it("returns meta after mint", async () => {
    const m = await mintFeed(SELF);
    const req = await signedFeedRequest("GET", `/feeds/${m.feed_id}/meta`, m.feed_id);
    const res = await SELF.fetch(req);
    expect(res.status).toBe(200);
    const meta = (await res.json()) as {
      created_at: number;
      expires_at: number;
    };
    expect(meta.created_at).toBe(m.created_at);
    expect(meta.expires_at).toBe(m.expires_at);
  });

  it("404 for unknown feed", async () => {
    const fakeId = randomBase64(9);
    const req = await signedFeedRequest("GET", `/feeds/${fakeId}/meta`, fakeId);
    const res = await SELF.fetch(req);
    expect(res.status).toBe(404);
  });

  it("rejects request signed with wrong feed id", async () => {
    const m = await mintFeed(SELF);
    // Sign with a DIFFERENT feed id but hit the real one's path → 401.
    const wrongFeed = randomBase64(9);
    const req = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/meta`,
      wrongFeed,
    );
    const res = await SELF.fetch(req);
    expect(res.status).toBe(401);
  });
});

describe("members lifecycle", () => {
  it("lists creator in members after mint", async () => {
    const m = await mintFeed(SELF, { name: "device-1" });
    const req = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/members`,
      m.feed_id,
    );
    const res = await SELF.fetch(req);
    expect(res.status).toBe(200);
    const data = (await res.json()) as {
      members: { member_id: string; name: string }[];
    };
    expect(data.members.length).toBe(1);
    expect(data.members[0].member_id).toBe(m.member_id);
    expect(data.members[0].name).toBe("device-1");
  });

  it("PUT adds a member, DELETE removes it", async () => {
    const m = await mintFeed(SELF);
    const newMemberId = randomMemberId();
    const card = {
      member_id: newMemberId,
      identity_pubkey: randomBase64(32),
      name: "device-2",
    };
    const putReq = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/members/${newMemberId}`,
      m.feed_id,
      { body: JSON.stringify(card) },
    );
    const putRes = await SELF.fetch(putReq);
    expect(putRes.status).toBe(204);

    const listReq = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/members`,
      m.feed_id,
    );
    const listRes = await SELF.fetch(listReq);
    const data = (await listRes.json()) as {
      members: { member_id: string }[];
    };
    expect(data.members.length).toBe(2);

    const delReq = await signedFeedRequest(
      "DELETE",
      `/feeds/${m.feed_id}/members/${newMemberId}`,
      m.feed_id,
    );
    const delRes = await SELF.fetch(delReq);
    expect(delRes.status).toBe(204);

    const listRes2 = await SELF.fetch(
      await signedFeedRequest("GET", `/feeds/${m.feed_id}/members`, m.feed_id),
    );
    const data2 = (await listRes2.json()) as {
      members: { member_id: string }[];
    };
    expect(data2.members.length).toBe(1);
  });

  it("rejects PUT with mismatched member_id in body", async () => {
    const m = await mintFeed(SELF);
    const newMemberId = randomMemberId();
    const card = {
      member_id: "different-from-url",
      identity_pubkey: randomBase64(32),
      name: "device-2",
    };
    const req = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/members/${newMemberId}`,
      m.feed_id,
      { body: JSON.stringify(card) },
    );
    const res = await SELF.fetch(req);
    expect(res.status).toBe(400);
  });
});

describe("slot lifecycle (multi-party broadcast)", () => {
  it("PUT then GET returns body and does NOT delete (broadcast)", async () => {
    const m = await mintFeed(SELF);
    const payload = new TextEncoder().encode("hello feed");
    const putReq = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/slots/slot-abc`,
      m.feed_id,
      { body: payload, contentLength: payload.byteLength },
    );
    const putRes = await SELF.fetch(putReq);
    expect(putRes.status).toBe(204);

    // First GET — should return body.
    const get1Req = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/slots/slot-abc`,
      m.feed_id,
    );
    const get1Res = await SELF.fetch(get1Req);
    expect(get1Res.status).toBe(200);
    expect(await get1Res.text()).toBe("hello feed");

    // Second GET — broadcast model means slot is still there.
    const get2Req = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/slots/slot-abc`,
      m.feed_id,
    );
    const get2Res = await SELF.fetch(get2Req);
    expect(get2Res.status).toBe(200);
    expect(await get2Res.text()).toBe("hello feed");
  });

  it("409 on duplicate slot id", async () => {
    const m = await mintFeed(SELF);
    const payload = new TextEncoder().encode("first");
    const req1 = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/slots/dup`,
      m.feed_id,
      { body: payload, contentLength: payload.byteLength },
    );
    expect((await SELF.fetch(req1)).status).toBe(204);

    const req2 = await signedFeedRequest(
      "PUT",
      `/feeds/${m.feed_id}/slots/dup`,
      m.feed_id,
      { body: payload, contentLength: payload.byteLength },
    );
    expect((await SELF.fetch(req2)).status).toBe(409);
  });

  it("lists slots since timestamp", async () => {
    const m = await mintFeed(SELF);
    const t0 = Math.floor(Date.now() / 1000);
    const body = new TextEncoder().encode("p");
    for (const id of ["a", "b", "c"]) {
      const req = await signedFeedRequest(
        "PUT",
        `/feeds/${m.feed_id}/slots/${id}`,
        m.feed_id,
        { body, contentLength: 1 },
      );
      await SELF.fetch(req);
    }
    const listReq = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/slots?since=${t0 - 1}`,
      m.feed_id,
    );
    const listRes = await SELF.fetch(listReq);
    expect(listRes.status).toBe(200);
    const data = (await listRes.json()) as {
      slots: { slot_id: string }[];
    };
    expect(data.slots.map((s) => s.slot_id).sort()).toEqual(["a", "b", "c"]);
  });
});

describe("extend / close", () => {
  it("extend updates expires_at", async () => {
    const m = await mintFeed(SELF, { ttlSeconds: 100 });
    const req = await signedFeedRequest(
      "POST",
      `/feeds/${m.feed_id}/extend`,
      m.feed_id,
      { body: JSON.stringify({ new_ttl_seconds: 7200 }) },
    );
    const res = await SELF.fetch(req);
    expect(res.status).toBe(200);
    const data = (await res.json()) as { expires_at: number };
    expect(data.expires_at).toBeGreaterThan(m.expires_at);
  });

  it("DELETE /feeds/:id purges meta + members + slots", async () => {
    const m = await mintFeed(SELF);
    const body = new TextEncoder().encode("p");
    await SELF.fetch(
      await signedFeedRequest(
        "PUT",
        `/feeds/${m.feed_id}/slots/zzz`,
        m.feed_id,
        { body, contentLength: 1 },
      ),
    );
    const delReq = await signedFeedRequest(
      "DELETE",
      `/feeds/${m.feed_id}`,
      m.feed_id,
    );
    const delRes = await SELF.fetch(delReq);
    expect(delRes.status).toBe(204);

    const metaReq = await signedFeedRequest(
      "GET",
      `/feeds/${m.feed_id}/meta`,
      m.feed_id,
    );
    const metaRes = await SELF.fetch(metaReq);
    expect(metaRes.status).toBe(404);
  });

  it("DELETE /feeds/:id 404s on an absent feed (not silently idempotent)", async () => {
    const fakeId = randomBase64(9);
    const delReq = await signedFeedRequest("DELETE", `/feeds/${fakeId}`, fakeId);
    const delRes = await SELF.fetch(delReq);
    expect(delRes.status).toBe(404);
  });
});

describe("legacy /slots/:id (unchanged)", () => {
  it("PUT then GET deletes the slot (delete-on-read)", async () => {
    const body = new TextEncoder().encode("legacy");
    const putReq = await signedHmacRequest("PUT", "/slots/legacy-test-1", {
      body,
      contentLength: body.byteLength,
    });
    expect((await SELF.fetch(putReq)).status).toBe(204);

    const get1 = await signedHmacRequest("GET", "/slots/legacy-test-1");
    const get1Res = await SELF.fetch(get1);
    expect(get1Res.status).toBe(200);

    // Second GET — gone.
    const get2 = await signedHmacRequest("GET", "/slots/legacy-test-1");
    const get2Res = await SELF.fetch(get2);
    expect(get2Res.status).toBe(404);
  });
});
