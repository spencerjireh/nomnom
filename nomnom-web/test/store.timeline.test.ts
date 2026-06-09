// Store-level coverage for the feed-timeline shell rebuild: timeline append /
// patch, selectFeed persistence, per-feed auto_save toggle, rename + remove
// migrations, and the tolerant legacy-feed loader. Crypto and relay calls are
// out of scope — those live in test/feeds.vectors.test.ts.

import { beforeEach, describe, expect, it } from "vitest";
import type { Feed, TimelineEntry } from "../src/types";

class MemoryStorage {
  private store = new Map<string, string>();
  get length(): number {
    return this.store.size;
  }
  clear(): void {
    this.store.clear();
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? this.store.get(key)! : null;
  }
  key(i: number): string | null {
    return Array.from(this.store.keys())[i] ?? null;
  }
  removeItem(key: string): void {
    this.store.delete(key);
  }
  setItem(key: string, value: string): void {
    this.store.set(key, value);
  }
}

let store: typeof import("../src/state/store");
let persistence: typeof import("../src/state/persistence").persistence;

beforeEach(async () => {
  (globalThis as unknown as { localStorage: MemoryStorage }).localStorage = new MemoryStorage();
  // Re-import so each test starts with a fresh zustand store backed by the
  // fresh memory storage. Vitest caches modules per-file, so we reset cache.
  const { vi } = await import("vitest");
  vi.resetModules();
  store = await import("../src/state/store");
  persistence = (await import("../src/state/persistence")).persistence;
});

function makeFeed(name: string, overrides: Partial<Feed> = {}): Feed {
  return {
    name,
    feed_id: `id-${name}`,
    feed_token: `tok-${name}`,
    url: `https://r.example/f/${name}`,
    expires_at: 4_000_000_000,
    joined_at: 1_700_000_000,
    member_id: `me-${name}`,
    members_cache: [],
    last_post_ts: 0,
    auto_save: false,
    ...overrides,
  };
}

function entry(id: string, overrides: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    id,
    kind: "receive",
    name: "x.txt",
    bytes: 10,
    at: 1_700_000_000_000,
    status: "held",
    peerName: "alice",
    ...overrides,
  };
}

describe("timeline actions", () => {
  it("appends entries newest-first and patches by id", () => {
    const s = store.useStore.getState();
    s.upsertFeed(makeFeed("a"));
    s.appendTimeline("a", entry("e1", { at: 1 }));
    s.appendTimeline("a", entry("e2", { at: 2 }));

    const rows = store.useStore.getState().timelines["a"];
    expect(rows.map((r) => r.id)).toEqual(["e2", "e1"]);

    s.patchTimelineEntry("a", "e1", { status: "saved", body: undefined });
    expect(store.useStore.getState().timelines["a"].find((r) => r.id === "e1")?.status).toBe(
      "saved",
    );
  });

  it("markFeedViewed records a fresh timestamp", () => {
    const s = store.useStore.getState();
    s.upsertFeed(makeFeed("a"));
    const before = Date.now();
    s.markFeedViewed("a");
    const after = Date.now();
    const t = store.useStore.getState().viewedAt["a"];
    expect(t).toBeGreaterThanOrEqual(before);
    expect(t).toBeLessThanOrEqual(after);
  });
});

describe("feed selection", () => {
  it("selectFeed updates state and persists", () => {
    const s = store.useStore.getState();
    s.upsertFeed(makeFeed("a"));
    s.upsertFeed(makeFeed("b"));
    s.selectFeed("b");

    expect(store.useStore.getState().selectedFeed).toBe("b");
    expect(persistence.loadLastSelectedFeed()).toBe("b");

    s.selectFeed(null);
    expect(persistence.loadLastSelectedFeed()).toBeNull();
  });

  it("hydrate seeds selectedFeed from persistence, ignoring stale names", () => {
    persistence.saveLastSelectedFeed("ghost");
    persistence.saveFeeds({ feeds: [makeFeed("a")] });
    // Save a matching identity so hydrate keeps it (schema check).
    persistence.saveIdentity({
      device_id: "00",
      name: "web-guest",
      sig_priv: "00".repeat(32),
      sig_pub: "00".repeat(32),
    });

    store.useStore.getState().hydrate();
    expect(store.useStore.getState().selectedFeed).toBeNull(); // ghost ignored

    persistence.saveLastSelectedFeed("a");
    store.useStore.getState().hydrate();
    expect(store.useStore.getState().selectedFeed).toBe("a");
  });
});

describe("per-feed auto_save toggle", () => {
  it("setFeedAutoSave flips the flag and persists", () => {
    const s = store.useStore.getState();
    s.upsertFeed(makeFeed("a", { auto_save: false }));
    s.setFeedAutoSave("a", true);

    expect(store.useStore.getState().feeds.find((f) => f.name === "a")?.auto_save).toBe(true);
    expect(persistence.loadFeeds().feeds[0].auto_save).toBe(true);
  });
});

describe("removeFeed cleans up associated state", () => {
  it("drops the timeline, viewedAt entry, and clears selection if matched", () => {
    const s = store.useStore.getState();
    s.upsertFeed(makeFeed("a"));
    s.appendTimeline("a", entry("e1"));
    s.markFeedViewed("a");
    s.selectFeed("a");

    s.removeFeed("a");
    const after = store.useStore.getState();
    expect(after.feeds).toHaveLength(0);
    expect(after.timelines["a"]).toBeUndefined();
    expect(after.viewedAt["a"]).toBeUndefined();
    expect(after.selectedFeed).toBeNull();
    expect(persistence.loadLastSelectedFeed()).toBeNull();
  });
});

describe("renameFeed migrates per-feed state", () => {
  it("moves timeline + viewedAt + selectedFeed onto the new name", () => {
    const s = store.useStore.getState();
    s.upsertFeed(makeFeed("a"));
    s.appendTimeline("a", entry("e1"));
    s.markFeedViewed("a");
    s.selectFeed("a");

    const ok = s.renameFeed("a", "b");
    expect(ok).toBe(true);
    const after = store.useStore.getState();
    expect(after.feeds[0].name).toBe("b");
    expect(after.timelines["b"]?.[0].id).toBe("e1");
    expect(after.timelines["a"]).toBeUndefined();
    expect(after.viewedAt["b"]).toBeDefined();
    expect(after.selectedFeed).toBe("b");
  });

  it("rejects a rename that would collide", () => {
    const s = store.useStore.getState();
    s.upsertFeed(makeFeed("a"));
    s.upsertFeed(makeFeed("b"));
    expect(s.renameFeed("a", "b")).toBe(false);
  });
});

describe("persistence migration", () => {
  it("loads legacy feeds without auto_save and defaults the flag to false", () => {
    localStorage.setItem(
      "nomnom:feeds",
      JSON.stringify({
        default: "a", // legacy field — should be ignored
        feeds: [
          {
            name: "a",
            feed_id: "id",
            feed_token: "tok",
            url: "https://r.example/f/tok",
            expires_at: 4_000_000_000,
            joined_at: 1,
            member_id: "me",
            members_cache: [],
            last_post_ts: 0,
            // no auto_save
          },
        ],
      }),
    );

    const { feeds } = persistence.loadFeeds();
    expect(feeds).toHaveLength(1);
    expect(feeds[0].auto_save).toBe(false);
  });

  it("round-trips a feed with auto_save=true through save+load", () => {
    persistence.saveFeeds({ feeds: [makeFeed("a", { auto_save: true })] });
    expect(persistence.loadFeeds().feeds[0].auto_save).toBe(true);
  });
});
