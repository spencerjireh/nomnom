// Unit tests for runHistory — the load-time timeline rebuild. The crypto worker
// is mocked (Node has no Worker) and the FeedClient is stubbed via the `ctx` test
// seam, mirroring test/receive.test.ts.

import { afterEach, describe, expect, it, vi } from "vitest";
import { runHistory, type HistoryParams } from "../src/orchestration/history";
import type { FeedContext } from "../src/orchestration/feed-actions";
import type { FeedClient, SlotMeta } from "../src/relay/feed-client";
import type { Feed } from "../src/types";

vi.mock("../src/worker/cryptoClient", () => ({
  cryptoClient: { feedOpen: vi.fn() },
}));
import { cryptoClient } from "../src/worker/cryptoClient";
const feedOpen = vi.mocked(cryptoClient.feedOpen);

const SELF_PUB = "cd".repeat(32);
const PEER_PUB = "ab".repeat(32);
const NOW = Math.floor(Date.now() / 1000);

function makeFeed(): Feed {
  return {
    name: "channel",
    feed_id: "feedtoken01",
    feed_token: "feedtoken01",
    url: "https://relay.test/f/feedtoken01",
    expires_at: 2_000_000_000,
    joined_at: 1_700_000_000,
    member_id: "me-member-id",
    members_cache: [
      { member_id: "me-member-id", identity_pubkey: SELF_PUB, name: "me" },
      { member_id: "peer-member-id", identity_pubkey: PEER_PUB, name: "bob" },
    ],
    last_post_ts: 0,
    auto_save: false,
  };
}

interface StubOpts {
  slots: SlotMeta[] | { error: Error };
  getSlot?: (slotId: string) => Promise<ArrayBuffer | null>;
}

function makeStub(opts: StubOpts) {
  const listSlots = vi.fn(() =>
    "error" in opts.slots
      ? Promise.reject((opts.slots as { error: Error }).error)
      : Promise.resolve(opts.slots as SlotMeta[]),
  );
  const listMembers = vi.fn(() => Promise.resolve(makeFeed().members_cache));
  const getSlot = vi.fn((_f: string, _k: Uint8Array, slotId: string) =>
    (opts.getSlot ?? (() => Promise.resolve(new ArrayBuffer(8))))(slotId),
  );
  const client = { listSlots, listMembers, getSlot };
  return { client: client as unknown as FeedClient, listSlots, listMembers, getSlot };
}

function makeParams(client: FeedClient): HistoryParams {
  const feed = makeFeed();
  const ctx: FeedContext = {
    feed,
    identity: { name: "me", device_id: "dev", sig_priv: "00", sig_pub: SELF_PUB },
    feedKey: new Uint8Array(32),
    feedKeyHex: "00".repeat(32),
    client,
  };
  return {
    feed,
    identity: ctx.identity,
    signal: new AbortController().signal,
    ctx,
  };
}

afterEach(() => {
  feedOpen.mockReset();
});

describe("runHistory", () => {
  it("rebuilds own posts as sent rows and others as held receives, newest-first", async () => {
    // s1 (older) is our own post; s2 (newer) is from bob.
    feedOpen
      .mockResolvedValueOnce({
        header: { smid: "me-member-id", fn: "mine.txt", fs: 4, sik: SELF_PUB, pa: NOW - 20 },
        body: new ArrayBuffer(4),
      } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>)
      .mockResolvedValueOnce({
        header: { smid: "peer-member-id", fn: "theirs.txt", fs: 3, sik: PEER_PUB, pa: NOW - 10 },
        body: new ArrayBuffer(3),
      } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>);

    const { client, getSlot } = makeStub({
      slots: [
        { slot_id: "s2", created_at: NOW - 10 },
        { slot_id: "s1", created_at: NOW - 20 }, // out of order on purpose
      ],
    });
    const { rows, maxCursor } = await runHistory(makeParams(client));

    expect(getSlot).toHaveBeenCalledTimes(2);
    // Newest-first: bob's receive, then our send.
    expect(rows.map((r) => ({ kind: r.kind, name: r.name }))).toEqual([
      { kind: "receive", name: "theirs.txt" },
      { kind: "send", name: "mine.txt" },
    ]);
    const recv = rows[0];
    expect(recv.status).toBe("held");
    expect(recv.peerName).toBe("bob");
    expect(recv.bytes).toBe(3);
    // Rebuilt receive rows keep only the slot_id (body fetched lazily on save),
    // so a refresh doesn't hold every retained file in memory at once.
    expect(recv.body).toBeUndefined();
    expect(recv.slot_id).toBe("s2");
    const sent = rows[1];
    expect(sent.status).toBe("served");
    expect(sent.body).toBeUndefined();
    expect(maxCursor).toBe(NOW - 10);
  });

  it("skips foreign/undecryptable posts but still reports the max cursor", async () => {
    feedOpen.mockRejectedValue(new Error("bad magic"));
    const { client } = makeStub({ slots: [{ slot_id: "s1", created_at: NOW - 5 }] });

    const { rows, maxCursor } = await runHistory(makeParams(client));
    expect(rows).toEqual([]);
    expect(maxCursor).toBe(NOW - 5); // cursor advances so the live watch won't re-fetch it
  });

  it("drops a post whose sender identity key changed (spoof)", async () => {
    feedOpen.mockResolvedValue({
      header: { smid: "peer-member-id", fn: "x.txt", fs: 3, sik: "99".repeat(32), pa: NOW - 5 },
      body: new ArrayBuffer(3),
    } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>);
    const { client } = makeStub({ slots: [{ slot_id: "s1", created_at: NOW - 5 }] });

    const { rows } = await runHistory(makeParams(client));
    expect(rows).toEqual([]);
  });

  it("returns no rows and preserves the cursor when listSlots fails", async () => {
    const { client, getSlot } = makeStub({ slots: { error: new Error("relay down") } });
    const p = makeParams(client);

    const { rows, maxCursor } = await runHistory(p);
    expect(rows).toEqual([]);
    expect(getSlot).not.toHaveBeenCalled();
    expect(maxCursor).toBe(p.feed.last_post_ts);
  });

  it("ignores posts older than the retention window", async () => {
    feedOpen.mockResolvedValue({
      header: { smid: "peer-member-id", fn: "recent.txt", fs: 3, sik: PEER_PUB, pa: NOW - 1 },
      body: new ArrayBuffer(3),
    } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>);
    const { client, getSlot } = makeStub({
      slots: [
        { slot_id: "ancient", created_at: 1 }, // ~1970, far outside the window
        { slot_id: "recent", created_at: NOW - 1 },
      ],
    });

    const { rows } = await runHistory(makeParams(client));
    // Only the recent slot is fetched + rebuilt; the ancient one is skipped.
    expect(getSlot).toHaveBeenCalledTimes(1);
    expect(getSlot).toHaveBeenCalledWith("feedtoken01", expect.anything(), "recent", expect.anything());
    expect(rows).toHaveLength(1);
    expect(rows[0].name).toBe("recent.txt");
  });

  it("bails without touching the store when the signal is already aborted", async () => {
    feedOpen.mockResolvedValue({
      header: { smid: "peer-member-id", fn: "x.txt", fs: 3, sik: PEER_PUB, pa: NOW - 5 },
      body: new ArrayBuffer(3),
    } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>);
    const { client, getSlot } = makeStub({ slots: [{ slot_id: "s1", created_at: NOW - 5 }] });
    const p = makeParams(client);
    const ac = new AbortController();
    ac.abort();
    p.signal = ac.signal;

    const { rows } = await runHistory(p);
    expect(getSlot).not.toHaveBeenCalled();
    expect(rows).toEqual([]);
  });
});
