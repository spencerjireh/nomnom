import { env, runInDurableObject, SELF } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import {
  BASE,
  feedStub,
  mintFeed,
  putSlot,
  randomBase64,
  randomMemberId,
  signedFeedRequest,
  signedHmacRequest,
} from "./helpers";

async function feedGet(feedId: string, sub: string): Promise<Response> {
  return SELF.fetch(await signedFeedRequest("GET", `/feeds/${feedId}${sub}`, feedId));
}

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

describe("GET /auth (relay-secret probe)", () => {
  it("returns 204 with a valid relay MAC", async () => {
    const res = await SELF.fetch(await signedHmacRequest("GET", "/auth"));
    expect(res.status).toBe(204);
  });

  it("401 bad-mac with the wrong secret", async () => {
    const res = await SELF.fetch(
      await signedHmacRequest("GET", "/auth", { secret: "nope" }),
    );
    expect(res.status).toBe(401);
    expect(((await res.json()) as { error: string }).error).toBe("bad-mac");
  });

  it("401 clock-skew with a stale timestamp", async () => {
    const res = await SELF.fetch(
      await signedHmacRequest("GET", "/auth", {
        tsOverride: Math.floor(Date.now() / 1000) - 600,
      }),
    );
    expect(res.status).toBe(401);
    expect(((await res.json()) as { error: string }).error).toBe("clock-skew");
  });

  it("405 on a non-GET method", async () => {
    const res = await SELF.fetch(await signedHmacRequest("POST", "/auth"));
    expect(res.status).toBe(405);
    expect(res.headers.get("Allow")).toContain("GET");
  });
});

describe("POST /feeds (mint)", () => {
  it("mints a feed and returns id + created_at", async () => {
    const minted = await mintFeed(SELF);
    expect(minted.feed_id).toMatch(/^[A-Za-z0-9_-]{12}$/);
    expect(minted.created_at).toBeGreaterThan(1_700_000_000);
  });

  it("rejects malformed member_card", async () => {
    const req = await signedHmacRequest("POST", "/feeds", {
      body: JSON.stringify({ member_card: { name: "x" } }),
    });
    const res = await SELF.fetch(req);
    expect(res.status).toBe(400);
  });

  it("rejects malformed JSON", async () => {
    const req = await signedHmacRequest("POST", "/feeds", { body: "not-json{" });
    const res = await SELF.fetch(req);
    expect(res.status).toBe(400);
  });

  it("413 on an oversized member card", async () => {
    const req = await signedHmacRequest("POST", "/feeds", {
      body: JSON.stringify({
        member_card: {
          member_id: randomMemberId(),
          identity_pubkey: "x".repeat(5000),
          name: "big",
        },
      }),
    });
    const res = await SELF.fetch(req);
    expect(res.status).toBe(413);
  });
});

describe("GET /feeds/:id/meta", () => {
  it("returns meta after mint", async () => {
    const m = await mintFeed(SELF);
    const res = await feedGet(m.feed_id, "/meta");
    expect(res.status).toBe(200);
    const meta = (await res.json()) as { created_at: number; last_used_at: number };
    expect(meta.created_at).toBe(m.created_at);
    expect(meta.last_used_at).toBeGreaterThanOrEqual(m.created_at);
  });

  it("404 for unknown feed", async () => {
    const fakeId = randomBase64(9);
    const res = await feedGet(fakeId, "/meta");
    expect(res.status).toBe(404);
  });

  it("rejects request signed with wrong feed id", async () => {
    const m = await mintFeed(SELF);
    // Sign with a DIFFERENT feed id but hit the real one's path → 401.
    const wrongFeed = randomBase64(9);
    const req = await signedFeedRequest("GET", `/feeds/${m.feed_id}/meta`, wrongFeed);
    const res = await SELF.fetch(req);
    expect(res.status).toBe(401);
  });
});

describe("members lifecycle", () => {
  it("lists creator in members after mint", async () => {
    const m = await mintFeed(SELF, { name: "device-1" });
    const res = await feedGet(m.feed_id, "/members");
    expect(res.status).toBe(200);
    const data = (await res.json()) as {
      members: { member_id: string; name: string; joined_at: number }[];
    };
    expect(data.members.length).toBe(1);
    expect(data.members[0].member_id).toBe(m.member_id);
    expect(data.members[0].name).toBe("device-1");
    expect(data.members[0].joined_at).toBe(m.created_at);
  });

  it("PUT adds a member, DELETE removes it", async () => {
    const m = await mintFeed(SELF);
    const newMemberId = randomMemberId();
    const card = {
      member_id: newMemberId,
      identity_pubkey: randomBase64(32),
      name: "device-2",
    };
    const putRes = await SELF.fetch(
      await signedFeedRequest(
        "PUT",
        `/feeds/${m.feed_id}/members/${newMemberId}`,
        m.feed_id,
        { body: JSON.stringify(card) },
      ),
    );
    expect(putRes.status).toBe(204);

    const data = (await (await feedGet(m.feed_id, "/members")).json()) as {
      members: { member_id: string }[];
    };
    expect(data.members.length).toBe(2);

    const delRes = await SELF.fetch(
      await signedFeedRequest(
        "DELETE",
        `/feeds/${m.feed_id}/members/${newMemberId}`,
        m.feed_id,
      ),
    );
    expect(delRes.status).toBe(204);

    const data2 = (await (await feedGet(m.feed_id, "/members")).json()) as {
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
    const res = await SELF.fetch(
      await signedFeedRequest(
        "PUT",
        `/feeds/${m.feed_id}/members/${newMemberId}`,
        m.feed_id,
        { body: JSON.stringify(card) },
      ),
    );
    expect(res.status).toBe(400);
  });

  it("409 feed-full on the 65th member; re-PUT of an existing member still 204", async () => {
    const m = await mintFeed(SELF);
    const ids: string[] = [];
    for (let i = 0; i < 63; i++) {
      const id = randomMemberId();
      ids.push(id);
      const res = await SELF.fetch(
        await signedFeedRequest(
          "PUT",
          `/feeds/${m.feed_id}/members/${id}`,
          m.feed_id,
          {
            body: JSON.stringify({
              member_id: id,
              identity_pubkey: randomBase64(32),
              name: `d${i}`,
            }),
          },
        ),
      );
      expect(res.status).toBe(204);
    }
    const extra = randomMemberId();
    const full = await SELF.fetch(
      await signedFeedRequest(
        "PUT",
        `/feeds/${m.feed_id}/members/${extra}`,
        m.feed_id,
        {
          body: JSON.stringify({
            member_id: extra,
            identity_pubkey: randomBase64(32),
            name: "one-too-many",
          }),
        },
      ),
    );
    expect(full.status).toBe(409);
    expect(((await full.json()) as { error: string }).error).toBe("feed-full");

    const rePut = await SELF.fetch(
      await signedFeedRequest(
        "PUT",
        `/feeds/${m.feed_id}/members/${ids[0]}`,
        m.feed_id,
        {
          body: JSON.stringify({
            member_id: ids[0],
            identity_pubkey: randomBase64(32),
            name: "renamed",
          }),
        },
      ),
    );
    expect(rePut.status).toBe(204);
  });

  it("wakes an in-flight members long-poll on a card update", async () => {
    const pubkey = randomBase64(32);
    const m = await mintFeed(SELF, { name: "old-name", identityPubkey: pubkey });
    // Anchor `since` at the creator's join second so the creator is not "fresh"
    // and the long-poll blocks instead of returning immediately.
    const since = m.created_at;
    const pollPromise = SELF.fetch(
      await signedFeedRequest(
        "GET",
        `/feeds/${m.feed_id}/members?since=${since}&wait=8000`,
        m.feed_id,
      ),
    );

    // Let the poll begin and cross a second boundary so the bumped joined_at
    // is strictly greater than `since`.
    await new Promise((r) => setTimeout(r, 1500));
    const renameRes = await SELF.fetch(
      await signedFeedRequest(
        "PUT",
        `/feeds/${m.feed_id}/members/${m.member_id}`,
        m.feed_id,
        {
          body: JSON.stringify({
            member_id: m.member_id,
            identity_pubkey: pubkey,
            name: "new-name",
          }),
        },
      ),
    );
    expect(renameRes.status).toBe(204);

    const res = await pollPromise;
    expect(res.status).toBe(200);
    const data = (await res.json()) as {
      members: { member_id: string; name: string }[];
      fresh: { member_id: string; name: string }[];
    };
    const creator = data.members.find((x) => x.member_id === m.member_id);
    expect(creator?.name).toBe("new-name");
    expect(data.fresh.some((x) => x.name === "new-name")).toBe(true);
  });
});

describe("slot lifecycle (multi-party broadcast)", () => {
  it("PUT then GET returns body and does NOT delete (broadcast)", async () => {
    const m = await mintFeed(SELF);
    expect((await putSlot(m.feed_id, "slot-abc", "hello feed")).status).toBe(204);

    const get1 = await feedGet(m.feed_id, "/slots/slot-abc");
    expect(get1.status).toBe(200);
    expect(await get1.text()).toBe("hello feed");

    const get2 = await feedGet(m.feed_id, "/slots/slot-abc");
    expect(get2.status).toBe(200);
    expect(await get2.text()).toBe("hello feed");
  });

  it("409 on duplicate slot id", async () => {
    const m = await mintFeed(SELF);
    expect((await putSlot(m.feed_id, "dup", "first")).status).toBe(204);
    expect((await putSlot(m.feed_id, "dup", "second")).status).toBe(409);
  });

  it("concurrent PUTs to one slot id yield exactly one 204 and one 409", async () => {
    const m = await mintFeed(SELF);
    const statuses = (
      await Promise.all([putSlot(m.feed_id, "race"), putSlot(m.feed_id, "race")])
    )
      .map((r) => r.status)
      .sort();
    // The DO's synchronous pre-check guarantees the loser gets 409 before it
    // touches R2, not a silent overwrite or a deleted winner.
    expect(statuses).toEqual([204, 409]);
  });

  it("rejects a malformed slot id with 400", async () => {
    const m = await mintFeed(SELF);
    const res = await feedGet(m.feed_id, "/slots/bad!id");
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("bad-slot-id");
  });

  it("lists slots since a seq cursor, in order", async () => {
    const m = await mintFeed(SELF);
    for (const id of ["a", "b", "c"]) await putSlot(m.feed_id, id);
    const res = await feedGet(m.feed_id, "/slots?since=0");
    expect(res.status).toBe(200);
    const data = (await res.json()) as {
      slots: { seq: number; slot_id: string; created_at: number }[];
    };
    expect(data.slots.map((s) => s.slot_id)).toEqual(["a", "b", "c"]);
    expect(data.slots.map((s) => s.seq)).toEqual([1, 2, 3]);

    const tail = (await (await feedGet(m.feed_id, "/slots?since=2")).json()) as {
      slots: { slot_id: string }[];
    };
    expect(tail.slots.map((s) => s.slot_id)).toEqual(["c"]);
  });

  it("slots long-poll returns [] at the deadline", async () => {
    const m = await mintFeed(SELF);
    const t0 = Date.now();
    const res = await feedGet(m.feed_id, "/slots?since=0&wait=300");
    expect(res.status).toBe(200);
    expect(((await res.json()) as { slots: unknown[] }).slots).toEqual([]);
    expect(Date.now() - t0).toBeGreaterThanOrEqual(250);
  });

  it("slots long-poll wakes on a new post", async () => {
    const m = await mintFeed(SELF);
    const t0 = Date.now();
    const pending = feedGet(m.feed_id, "/slots?since=0&wait=8000");
    await new Promise((r) => setTimeout(r, 200));
    expect((await putSlot(m.feed_id, "late")).status).toBe(204);
    const res = await pending;
    const data = (await res.json()) as { slots: { slot_id: string }[] };
    expect(data.slots.map((s) => s.slot_id)).toEqual(["late"]);
    expect(Date.now() - t0).toBeLessThan(2000);
  });

  it("GET /slots/:id 404s on a never-minted feed", async () => {
    const fakeId = randomBase64(9);
    const res = await feedGet(fakeId, "/slots/anything");
    expect(res.status).toBe(404);
  });

  it("DELETE /slots/:id removes the row and the blob, then 404s", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "keep");
    await putSlot(m.feed_id, "gone");
    const key = `feeds/${m.feed_id}/slots/gone`;
    expect(await env.BUCKET.head(key)).not.toBeNull();

    const del = await SELF.fetch(
      await signedFeedRequest("DELETE", `/feeds/${m.feed_id}/slots/gone`, m.feed_id),
    );
    expect(del.status).toBe(204);
    expect(await env.BUCKET.head(key)).toBeNull();

    const again = await SELF.fetch(
      await signedFeedRequest("DELETE", `/feeds/${m.feed_id}/slots/gone`, m.feed_id),
    );
    expect(again.status).toBe(404);
    expect((await feedGet(m.feed_id, "/slots/gone")).status).toBe(404);

    const list = (await (await feedGet(m.feed_id, "/slots?since=0")).json()) as {
      slots: { slot_id: string }[];
    };
    expect(list.slots.map((s) => s.slot_id)).toEqual(["keep"]);
  });
});

describe("close", () => {
  it("DELETE /feeds/:id purges rows, blobs, and DO storage", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "zzz");
    const key = `feeds/${m.feed_id}/slots/zzz`;
    expect(await env.BUCKET.head(key)).not.toBeNull();

    const delRes = await SELF.fetch(
      await signedFeedRequest("DELETE", `/feeds/${m.feed_id}`, m.feed_id),
    );
    expect(delRes.status).toBe(204);

    expect((await feedGet(m.feed_id, "/meta")).status).toBe(404);
    expect(await env.BUCKET.head(key)).toBeNull();
    await runInDurableObject(feedStub(m.feed_id), async (_instance, state) => {
      const tables = state.storage.sql
        .exec("SELECT name FROM sqlite_master WHERE type='table' AND name='feed'")
        .toArray();
      expect(tables.length).toBe(0);
      expect(await state.storage.getAlarm()).toBeNull();
    });
  });

  it("DELETE /feeds/:id 404s on an absent feed (not silently idempotent)", async () => {
    const fakeId = randomBase64(9);
    const delRes = await SELF.fetch(
      await signedFeedRequest("DELETE", `/feeds/${fakeId}`, fakeId),
    );
    expect(delRes.status).toBe(404);
  });
});

describe("malformed ids + removed routes", () => {
  it("rejects a malformed feed id with 400 (before auth)", async () => {
    // The feed-id grammar check runs before signature verification, so no
    // auth header is needed to exercise it.
    const res = await SELF.fetch(`${BASE}/feeds/x/meta`);
    expect(res.status).toBe(400);
    expect(((await res.json()) as { error: string }).error).toBe("bad-feed-id");
  });

  it("legacy /slots/* is gone", async () => {
    const res = await SELF.fetch(await signedHmacRequest("GET", "/slots/whatever"));
    expect(res.status).toBe(404);
  });

  it("/feeds/:id/extend and /stream are gone", async () => {
    const m = await mintFeed(SELF);
    const ext = await SELF.fetch(
      await signedFeedRequest("POST", `/feeds/${m.feed_id}/extend`, m.feed_id, {
        body: "{}",
      }),
    );
    expect(ext.status).toBe(404);
    expect((await feedGet(m.feed_id, "/stream")).status).toBe(404);
  });
});
