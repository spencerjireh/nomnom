// Unit tests for runReceive — the receive watch. The crypto worker is mocked
// (Node has no Worker) and the FeedClient is stubbed via the `ctx` test seam
// with a push-driven fake `watch` generator, so tests hand it frames directly.

import { afterEach, describe, expect, it, vi } from "vitest";
import { runReceive, type ReceiveParams } from "../src/orchestration/receive";
import type { FeedContext } from "../src/orchestration/feed-actions";
import type { FeedClient, FeedFrame } from "../src/relay/feed-client";
import { WS_RECONNECT_MIN_MS } from "../src/config";
import type { Feed, Member } from "../src/types";

vi.mock("../src/worker/cryptoClient", () => ({
  cryptoClient: { feedOpen: vi.fn() },
}));
import { cryptoClient } from "../src/worker/cryptoClient";
const feedOpen = vi.mocked(cryptoClient.feedOpen);

const SELF_PUB = "cd".repeat(32);
const PEER_PUB = "ab".repeat(32);
const NEW_PUB = "ef".repeat(32);

function makeFeed(): Feed {
  return {
    name: "channel",
    feed_id: "feedtoken01",
    feed_token: "feedtoken01",
    url: "https://relay.test/f/feedtoken01",
    joined_at: 1_700_000_000,
    member_id: "me-member-id",
    members_cache: [
      { member_id: "me-member-id", identity_pubkey: SELF_PUB, name: "me" },
      { member_id: "peer-member-id", identity_pubkey: PEER_PUB, name: "bob" },
    ],
    last_seq: 0,
    auto_save: false,
  };
}

/** A frame source the test pushes into. Each `watch` call gets a fresh
 * generator that drains `push`ed frames, then hangs until `end()` (which ends
 * the current generator — simulating a socket drop) or the signal aborts. */
function makeFrameSource() {
  let queue: FeedFrame[] = [];
  let wake: (() => void) | null = null;
  let ended = false;
  const opens: number[] = []; // getSince() at each (re)connect
  const ping = () => {
    const w = wake;
    wake = null;
    w?.();
  };
  return {
    opens,
    push(f: FeedFrame) {
      queue.push(f);
      ping();
    },
    end() {
      ended = true;
      ping();
    },
    async *watch(
      _feedId: string,
      _key: Uint8Array,
      opts: { getSince: () => number; signal: AbortSignal },
    ): AsyncGenerator<FeedFrame> {
      opens.push(opts.getSince());
      ended = false;
      queue = [];
      const onAbort = () => ping();
      opts.signal.addEventListener("abort", onAbort, { once: true });
      try {
        while (!opts.signal.aborted && !ended) {
          if (queue.length === 0) {
            await new Promise<void>((r) => (wake = r));
            continue;
          }
          yield queue.shift()!;
        }
      } finally {
        opts.signal.removeEventListener("abort", onAbort);
      }
    },
  };
}

function makeStub(opts: { getSlot?: (slotId: string) => Promise<ArrayBuffer | null> } = {}) {
  const source = makeFrameSource();
  const listMembers = vi.fn(() => Promise.resolve(makeFeed().members_cache));
  const getSlot = vi.fn((_f: string, _k: Uint8Array, slotId: string) =>
    (opts.getSlot ?? (() => Promise.resolve(new ArrayBuffer(8))))(slotId),
  );
  const client = { listMembers, getSlot, watch: source.watch.bind(source) };
  return { client: client as unknown as FeedClient, source, listMembers, getSlot };
}

type Params = ReceiveParams & {
  onFile: ReturnType<typeof vi.fn>;
  onAdvance: ReturnType<typeof vi.fn>;
  onRoster: ReturnType<typeof vi.fn>;
  onDeleted: ReturnType<typeof vi.fn>;
};

function makeParams(
  client: FeedClient,
  signal: AbortSignal,
  hooks: Partial<ReceiveParams["hooks"]> = {},
): Params {
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
    hooks: { isPinned: () => true, onTofu: async () => true, pinPeer: vi.fn(), ...hooks },
    onFile: vi.fn(),
    onAdvance: vi.fn(),
    onRoster: vi.fn(),
    onDeleted: vi.fn(),
    signal,
    ctx,
  };
}

function bobPost(fn = "x.txt") {
  feedOpen.mockResolvedValue({
    header: { smid: "peer-member-id", fn, sik: PEER_PUB },
    body: new ArrayBuffer(3),
  } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>);
}

afterEach(() => {
  vi.useRealTimers();
  feedOpen.mockReset();
});

describe("runReceive (WebSocket frames)", () => {
  it("refreshes the roster once up front, then relies on member frames", async () => {
    vi.useFakeTimers();
    const { client, listMembers } = makeStub();
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    const done = runReceive(p);

    await vi.advanceTimersByTimeAsync(0);
    expect(listMembers).toHaveBeenCalledTimes(1);
    expect(p.onRoster).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(listMembers).toHaveBeenCalledTimes(1); // no polling loop

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(done).resolves.toBe(0);
  });

  it("delivers a post frame with its slot_id and advances the seq cursor", async () => {
    vi.useFakeTimers();
    bobPost();
    const { client, source } = makeStub();
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    const done = runReceive(p);
    await vi.advanceTimersByTimeAsync(0);

    source.push({ type: "post", seq: 5, slot_id: "s1", created_at: 100 });
    await vi.advanceTimersByTimeAsync(0);
    expect(p.onFile).toHaveBeenCalledTimes(1);
    expect(p.onFile).toHaveBeenCalledWith(
      expect.objectContaining({ name: "x.txt", bytes: 3, peerName: "bob", slot_id: "s1" }),
    );
    expect(p.onAdvance).toHaveBeenCalledWith(5);

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(done).resolves.toBe(1);
  });

  it("skips our own broadcast but advances the cursor", async () => {
    vi.useFakeTimers();
    feedOpen.mockResolvedValue({
      header: { smid: "me-member-id", fn: "x.txt", sik: SELF_PUB },
      body: new ArrayBuffer(3),
    } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>);
    const { client, source } = makeStub();
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    const done = runReceive(p);
    await vi.advanceTimersByTimeAsync(0);

    source.push({ type: "post", seq: 7, slot_id: "s1", created_at: 100 });
    await vi.advanceTimersByTimeAsync(0);
    expect(p.onFile).not.toHaveBeenCalled();
    expect(p.onAdvance).toHaveBeenCalledWith(7);

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(done).resolves.toBe(0);
  });

  it("dedups frames at or below the cursor", async () => {
    vi.useFakeTimers();
    bobPost();
    const { client, source, getSlot } = makeStub();
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    p.feed = { ...p.feed, last_seq: 3 };
    const done = runReceive(p);
    await vi.advanceTimersByTimeAsync(0);

    source.push({ type: "post", seq: 3, slot_id: "old", created_at: 1 });
    source.push({ type: "post", seq: 2, slot_id: "older", created_at: 1 });
    source.push({ type: "post", seq: 4, slot_id: "new", created_at: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(getSlot).toHaveBeenCalledTimes(1);
    expect(getSlot).toHaveBeenCalledWith("feedtoken01", expect.anything(), "new", expect.anything());
    expect(p.onAdvance).toHaveBeenCalledWith(4);

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await done;
  });

  it("drops the socket on a body-fetch failure and reconnects from the old cursor", async () => {
    vi.useFakeTimers();
    bobPost();
    let fail = true;
    const { client, source } = makeStub({
      getSlot: () => (fail ? Promise.reject(new Error("network down")) : Promise.resolve(new ArrayBuffer(8))),
    });
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    const done = runReceive(p);
    await vi.advanceTimersByTimeAsync(0);
    expect(source.opens).toEqual([0]);

    // Two frames arrive; the first fails to fetch. The second must NOT be
    // processed ahead of it (that would advance past the failed post).
    source.push({ type: "post", seq: 9, slot_id: "s1", created_at: 1 });
    source.push({ type: "post", seq: 10, slot_id: "s2", created_at: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(p.onFile).not.toHaveBeenCalled();
    expect(p.onAdvance).not.toHaveBeenCalled();

    // After the reconnect backoff, watch is re-entered with the un-advanced cursor.
    fail = false;
    await vi.advanceTimersByTimeAsync(WS_RECONNECT_MIN_MS);
    expect(source.opens).toEqual([0, 0]);
    source.push({ type: "post", seq: 9, slot_id: "s1", created_at: 1 });
    source.push({ type: "post", seq: 10, slot_id: "s2", created_at: 1 });
    await vi.advanceTimersByTimeAsync(0);
    expect(p.onFile).toHaveBeenCalledTimes(2);
    expect(p.onAdvance).toHaveBeenLastCalledWith(10);

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(done).resolves.toBe(2);
  });

  it("runs TOFU on an unseen member join and updates the roster; leave removes", async () => {
    vi.useFakeTimers();
    const onTofu = vi.fn(async () => true);
    const pinPeer = vi.fn();
    const { client, source } = makeStub();
    const ac = new AbortController();
    const p = makeParams(client, ac.signal, { isPinned: () => false, onTofu, pinPeer });
    const done = runReceive(p);
    await vi.advanceTimersByTimeAsync(0);
    onTofu.mockClear(); // ignore anything the initial refresh did
    pinPeer.mockClear();

    const carol: Member = { member_id: "carol-id", identity_pubkey: NEW_PUB, name: "carol", joined_at: 5 };
    source.push({ type: "member", action: "join", member: carol });
    await vi.advanceTimersByTimeAsync(0);
    expect(onTofu).toHaveBeenCalledTimes(1);
    expect(onTofu).toHaveBeenCalledWith(expect.objectContaining({ peerName: "carol", sigPub: NEW_PUB }));
    expect(pinPeer).toHaveBeenCalledWith(NEW_PUB, "carol");
    const rosterAfterJoin = p.onRoster.mock.calls.at(-1)![0] as Member[];
    expect(rosterAfterJoin.map((m) => m.member_id)).toContain("carol-id");

    // A repeat join (rename) does not re-prompt.
    source.push({ type: "member", action: "join", member: { ...carol, name: "carol2" } });
    await vi.advanceTimersByTimeAsync(0);
    expect(onTofu).toHaveBeenCalledTimes(1);

    source.push({ type: "member", action: "leave", member: carol });
    await vi.advanceTimersByTimeAsync(0);
    const rosterAfterLeave = p.onRoster.mock.calls.at(-1)![0] as Member[];
    expect(rosterAfterLeave.map((m) => m.member_id)).not.toContain("carol-id");

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await done;
  });

  it("forwards deleted frames", async () => {
    vi.useFakeTimers();
    const { client, source } = makeStub();
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    const done = runReceive(p);
    await vi.advanceTimersByTimeAsync(0);

    source.push({ type: "deleted", slot_id: "gone" });
    await vi.advanceTimersByTimeAsync(0);
    expect(p.onDeleted).toHaveBeenCalledWith("gone");

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await done;
  });
});
