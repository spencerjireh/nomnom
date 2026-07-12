// Store-level coverage for the single-channel shell: timeline append / patch,
// channel set/patch/leave persistence, the auto_save toggle, re-pair clearing
// the timeline, and the tolerant channel loader (incl. legacy-feeds migration).
// Crypto and relay calls are out of scope — those live in test/feeds.vectors.test.ts.

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
    s.setChannel(makeFeed("channel"));
    s.appendTimeline(entry("e1", { at: 1 }));
    s.appendTimeline(entry("e2", { at: 2 }));

    const rows = store.useStore.getState().timeline;
    expect(rows.map((r) => r.id)).toEqual(["e2", "e1"]);

    s.patchTimelineEntry("e1", { status: "saved", body: undefined });
    expect(store.useStore.getState().timeline.find((r) => r.id === "e1")?.status).toBe("saved");
  });

  it("setTimeline replaces the whole timeline wholesale (idempotent rebuild)", () => {
    const s = store.useStore.getState();
    s.setChannel(makeFeed("channel"));
    s.appendTimeline(entry("stale"));

    const rebuilt = [entry("r2", { at: 2 }), entry("r1", { at: 1 })];
    s.setTimeline(rebuilt);
    expect(store.useStore.getState().timeline.map((r) => r.id)).toEqual(["r2", "r1"]);

    // Running it again with the same rows is a no-op in effect (no duplication).
    s.setTimeline(rebuilt);
    expect(store.useStore.getState().timeline.map((r) => r.id)).toEqual(["r2", "r1"]);
  });

  it("removes an entry by id and leaves the rest", () => {
    const s = store.useStore.getState();
    s.setChannel(makeFeed("channel"));
    s.appendTimeline(entry("e1"));
    s.appendTimeline(entry("e2"));

    s.removeTimelineEntry("e1");
    expect(store.useStore.getState().timeline.map((r) => r.id)).toEqual(["e2"]);

    s.removeTimelineEntry("nope"); // unknown id is a no-op
    expect(store.useStore.getState().timeline).toHaveLength(1);
  });
});

describe("channel set/patch/leave", () => {
  it("setChannel stores and persists the channel", () => {
    const s = store.useStore.getState();
    s.setChannel(makeFeed("channel"));
    expect(store.useStore.getState().channel?.feed_id).toBe("id-channel");
    expect(persistence.loadChannel()?.feed_id).toBe("id-channel");
  });

  it("re-pairing to a different channel clears the timeline", () => {
    const s = store.useStore.getState();
    s.setChannel(makeFeed("channel"));
    s.appendTimeline(entry("e1"));
    expect(store.useStore.getState().timeline).toHaveLength(1);

    s.setChannel(makeFeed("channel", { feed_id: "id-other" }));
    expect(store.useStore.getState().timeline).toHaveLength(0);
  });

  it("leaveChannel clears the channel, timeline, and persistence", () => {
    const s = store.useStore.getState();
    s.setChannel(makeFeed("channel"));
    s.appendTimeline(entry("e1"));

    s.leaveChannel();
    const after = store.useStore.getState();
    expect(after.channel).toBeNull();
    expect(after.timeline).toHaveLength(0);
    expect(persistence.loadChannel()).toBeNull();
  });

  it("setAutoSave flips the flag and persists", () => {
    const s = store.useStore.getState();
    s.setChannel(makeFeed("channel", { auto_save: false }));
    s.setAutoSave(true);

    expect(store.useStore.getState().channel?.auto_save).toBe(true);
    expect(persistence.loadChannel()?.auto_save).toBe(true);
  });
});

describe("hydrate", () => {
  it("seeds the channel from persistence", () => {
    persistence.saveChannel(makeFeed("channel"));
    persistence.saveIdentity({
      device_id: "00",
      name: "web-guest",
      sig_priv: "00".repeat(32),
      sig_pub: "00".repeat(32),
    });

    store.useStore.getState().hydrate();
    expect(store.useStore.getState().channel?.feed_id).toBe("id-channel");
  });
});

describe("persistence migration", () => {
  it("loads a channel without auto_save and defaults the flag to false", () => {
    localStorage.setItem(
      "nomnom:channel",
      JSON.stringify({
        name: "channel",
        feed_id: "id",
        feed_token: "tok",
        url: "https://r.example/f/tok",
        expires_at: 4_000_000_000,
        joined_at: 1,
        member_id: "me",
        members_cache: [],
        last_post_ts: 0,
        // no auto_save
      }),
    );

    const ch = persistence.loadChannel();
    expect(ch?.auto_save).toBe(false);
  });

  it("migrates the first feed out of a legacy nomnom:feeds array", () => {
    localStorage.setItem(
      "nomnom:feeds",
      JSON.stringify({
        default: "home", // legacy field — ignored
        feeds: [
          {
            name: "home",
            feed_id: "legacy-id",
            feed_token: "tok",
            url: "https://r.example/f/tok",
            expires_at: 4_000_000_000,
            joined_at: 1,
            member_id: "me",
            members_cache: [],
            last_post_ts: 0,
            auto_save: true,
          },
        ],
      }),
    );

    const ch = persistence.loadChannel();
    expect(ch?.feed_id).toBe("legacy-id");
    expect(ch?.auto_save).toBe(true);
  });

  it("round-trips a channel with auto_save=true through save+load", () => {
    persistence.saveChannel(makeFeed("channel", { auto_save: true }));
    expect(persistence.loadChannel()?.auto_save).toBe(true);
  });
});

describe("rail collapsed preference", () => {
  it("defaults to false, round-trips, and is wiped by reset", () => {
    expect(persistence.loadRailCollapsed()).toBe(false);

    persistence.saveRailCollapsed(true);
    expect(persistence.loadRailCollapsed()).toBe(true);

    persistence.reset();
    expect(persistence.loadRailCollapsed()).toBe(false);
  });
});
