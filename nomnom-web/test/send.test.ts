// Unit test for runSend's progress reporting. The crypto worker and the feed
// context/roster are mocked so we can drive the seal phase's progress callback
// (which ends at 1.0) and assert the overall progress the composer sees never
// regresses.

import { describe, expect, it, vi } from "vitest";

vi.mock("../src/worker/cryptoClient", () => ({
  cryptoClient: { feedSeal: vi.fn() },
}));
vi.mock("../src/orchestration/feed-actions", () => ({
  feedContext: vi.fn(),
  refreshRoster: vi.fn(),
}));

import { runSend } from "../src/orchestration/send";
import { cryptoClient } from "../src/worker/cryptoClient";
import { feedContext, refreshRoster } from "../src/orchestration/feed-actions";
import type { FeedContext } from "../src/orchestration/feed-actions";
import type { Feed, Identity, Member } from "../src/types";

const feedSeal = vi.mocked(cryptoClient.feedSeal);
const mockedContext = vi.mocked(feedContext);
const mockedRefresh = vi.mocked(refreshRoster);

function makeFeed(): Feed {
  return {
    name: "channel",
    feed_id: "feedtoken01",
    feed_token: "feedtoken01",
    url: "https://relay.test/f/feedtoken01",
    expires_at: 2_000_000_000,
    joined_at: 1_700_000_000,
    member_id: "me",
    members_cache: [],
    last_post_ts: 0,
    auto_save: false,
  };
}

const identity: Identity = { name: "me", device_id: "dev", sig_priv: "00", sig_pub: "aa" };

describe("runSend progress", () => {
  it("never regresses and ends at 1 (seal peaks at the upload boundary)", async () => {
    const putSlot = vi.fn(() => Promise.resolve());
    mockedContext.mockReturnValue({
      feed: makeFeed(),
      identity,
      feedKey: new Uint8Array(32),
      feedKeyHex: "00".repeat(32),
      client: { putSlot } as unknown as FeedContext["client"],
    });
    const roster: Member[] = [
      { member_id: "me", identity_pubkey: "aa", name: "me" },
      { member_id: "other", identity_pubkey: "bb", name: "bob" },
    ];
    mockedRefresh.mockResolvedValue(roster);
    // The XOR pass drives its own fraction up to 1.0 — the pre-fix source of the
    // backward jump when the upload phase then reset it to 0.95.
    feedSeal.mockImplementation(async (_req, onProgress) => {
      onProgress?.("xor", 0.5);
      onProgress?.("xor", 1);
      return new ArrayBuffer(8);
    });

    const seen: number[] = [];
    const result = await runSend({
      feed: makeFeed(),
      identity,
      payload: { name: "x.txt", data: new ArrayBuffer(16) },
      hooks: {
        isPinned: () => true,
        onTofu: async () => true,
        pinPeer: () => {},
      },
      onProgress: (f) => seen.push(f),
      signal: new AbortController().signal,
    });

    expect(result.recipients).toBe(1);
    expect(seen.length).toBeGreaterThan(0);
    for (let i = 1; i < seen.length; i++) {
      expect(seen[i]).toBeGreaterThanOrEqual(seen[i - 1]);
    }
    expect(seen[seen.length - 1]).toBe(1);
    // The seal phase must stay within its sub-range, not touch 1.0 early.
    const beforeDone = seen.filter((f) => f < 1);
    expect(Math.max(...beforeDone)).toBeLessThanOrEqual(0.9);
  });
});
