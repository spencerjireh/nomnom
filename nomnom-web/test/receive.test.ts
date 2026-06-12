// Unit tests for runReceive — the receive watch. The crypto worker is mocked
// (Node has no Worker) and the FeedClient is stubbed via the `ctx` test seam.
// streamSlotEvents throws StreamUnsupportedError so every test exercises the
// long-poll fallback path.

import { afterEach, describe, expect, it, vi } from "vitest";
import { runReceive, type ReceiveParams } from "../src/orchestration/receive";
import type { FeedContext } from "../src/orchestration/feed-actions";
import type { FeedClient, SlotMeta } from "../src/relay/feed-client";
import { StreamUnsupportedError } from "../src/relay/errors";
import { RELAY_WAIT_MS } from "../src/config";
import type { Feed } from "../src/types";

vi.mock("../src/worker/cryptoClient", () => ({
  cryptoClient: { feedOpen: vi.fn() },
}));
import { cryptoClient } from "../src/worker/cryptoClient";
const feedOpen = vi.mocked(cryptoClient.feedOpen);

const PEER_PUB = "ab".repeat(32);

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
      { member_id: "me-member-id", identity_pubkey: "cd".repeat(32), name: "me" },
      { member_id: "peer-member-id", identity_pubkey: PEER_PUB, name: "bob" },
    ],
    last_post_ts: 0,
    auto_save: false,
  };
}

interface StubOpts {
  /** Outcomes for successive listSlots calls; afterwards it stays pending
   * until the signal aborts (mirroring a held long-poll fetch). */
  listSlots: Array<{ slots: SlotMeta[] } | { error: Error }>;
  getSlot?: (slotId: string) => Promise<ArrayBuffer | null>;
}

function makeStub(opts: StubOpts) {
  let call = 0;
  const listSlots = vi.fn(
    (_feedId: string, _key: Uint8Array, o: { signal?: AbortSignal }) => {
      const outcome = opts.listSlots[call++];
      if (outcome === undefined) {
        // Held long-poll: reject when the watch aborts, like a real fetch.
        return new Promise<SlotMeta[]>((_resolve, reject) => {
          o.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        });
      }
      return "error" in outcome
        ? Promise.reject(outcome.error)
        : Promise.resolve(outcome.slots);
    },
  );
  const listMembers = vi.fn(() => Promise.resolve(makeFeed().members_cache));
  const getSlot = vi.fn((_f: string, _k: Uint8Array, slotId: string) =>
    (opts.getSlot ?? (() => Promise.resolve(new ArrayBuffer(8))))(slotId),
  );
  const client = {
    listSlots,
    listMembers,
    getSlot,
    async *streamSlotEvents(): AsyncGenerator<SlotMeta> {
      throw new StreamUnsupportedError();
    },
  };
  return { client: client as unknown as FeedClient, listSlots, listMembers, getSlot };
}

function makeParams(
  client: FeedClient,
  signal: AbortSignal,
): ReceiveParams & { onFile: ReturnType<typeof vi.fn>; onAdvance: ReturnType<typeof vi.fn> } {
  const feed = makeFeed();
  const ctx: FeedContext = {
    feed,
    identity: { name: "me", device_id: "dev", sig_priv: "00", sig_pub: "cd".repeat(32) },
    feedKey: new Uint8Array(32),
    feedKeyHex: "00".repeat(32),
    client,
  };
  return {
    feed,
    identity: ctx.identity,
    hooks: { isPinned: () => true, onTofu: async () => true, pinPeer: vi.fn() },
    onFile: vi.fn(),
    onAdvance: vi.fn(),
    signal,
    ctx,
  };
}

afterEach(() => {
  vi.useRealTimers();
  feedOpen.mockReset();
});

describe("runReceive (long-poll fallback)", () => {
  it("survives a transient listSlots failure and still delivers (regression)", async () => {
    vi.useFakeTimers();
    feedOpen.mockResolvedValue({
      header: { smid: "peer-member-id", fn: "x.txt", sik: PEER_PUB },
      body: new ArrayBuffer(3),
    } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>);

    const { client, listSlots, listMembers } = makeStub({
      listSlots: [
        { error: new Error("relay hiccup") },
        { slots: [{ slot_id: "s1", created_at: 5 }] },
      ],
    });
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    const done = runReceive(p);

    // First listSlots rejects; pre-fix this rejection killed the whole watch.
    await vi.advanceTimersByTimeAsync(0);
    expect(listSlots).toHaveBeenCalledTimes(1);

    // After the backoff, the loop retries and the slot is delivered.
    await vi.advanceTimersByTimeAsync(RELAY_WAIT_MS);
    expect(p.onFile).toHaveBeenCalledTimes(1);
    expect(p.onFile).toHaveBeenCalledWith(
      expect.objectContaining({ name: "x.txt", bytes: 3, peerName: "bob" }),
    );
    expect(p.onAdvance).toHaveBeenCalledWith(5);

    // The roster loop was never orphaned: it keeps polling after the failure.
    const rosterCallsAtFailure = listMembers.mock.calls.length;
    await vi.advanceTimersByTimeAsync(RELAY_WAIT_MS);
    expect(listMembers.mock.calls.length).toBeGreaterThan(rosterCallsAtFailure);

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(done).resolves.toBe(1);
  });

  it("skips our own broadcast but advances the cursor", async () => {
    vi.useFakeTimers();
    feedOpen.mockResolvedValue({
      header: { smid: "me-member-id", fn: "x.txt", sik: "cd".repeat(32) },
      body: new ArrayBuffer(3),
    } as Awaited<ReturnType<typeof cryptoClient.feedOpen>>);

    const { client } = makeStub({
      listSlots: [{ slots: [{ slot_id: "s1", created_at: 7 }] }],
    });
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    const done = runReceive(p);

    await vi.advanceTimersByTimeAsync(0);
    expect(p.onFile).not.toHaveBeenCalled();
    expect(p.onAdvance).toHaveBeenCalledWith(7);

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(done).resolves.toBe(0);
  });

  it("does not advance the cursor when the slot fetch fails", async () => {
    vi.useFakeTimers();
    const { client } = makeStub({
      listSlots: [{ slots: [{ slot_id: "s1", created_at: 9 }] }],
      getSlot: () => Promise.reject(new Error("network down")),
    });
    const ac = new AbortController();
    const p = makeParams(client, ac.signal);
    const done = runReceive(p);

    await vi.advanceTimersByTimeAsync(0);
    expect(p.onFile).not.toHaveBeenCalled();
    expect(p.onAdvance).not.toHaveBeenCalled(); // contract: hiccup must not drop the post

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(done).resolves.toBe(0);
  });
});
