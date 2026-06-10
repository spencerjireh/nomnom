// Unit tests for FeedClient.streamSlotEvents — the SSE slot-discovery generator.
// EventSource doesn't exist in the node test env, so we install a controllable
// fake and drive its callbacks.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedClient } from "../src/relay/feed-client";
import { StreamUnsupportedError } from "../src/relay/errors";

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static last(): FakeEventSource {
    return FakeEventSource.instances[FakeEventSource.instances.length - 1];
  }
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }
  close(): void {
    this.closed = true;
  }
  // test drivers
  open(): void {
    this.onopen?.();
  }
  emit(data: unknown): void {
    this.onmessage?.({ data: JSON.stringify(data) });
  }
  emitRaw(data: string): void {
    this.onmessage?.({ data });
  }
  error(): void {
    this.onerror?.();
  }
}

const KEY = new Uint8Array(32);
const FEED = "feedtoken01";

beforeEach(() => {
  FakeEventSource.instances = [];
  (globalThis as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
});

afterEach(() => {
  delete (globalThis as unknown as { EventSource?: unknown }).EventSource;
});

describe("FeedClient.streamSlotEvents", () => {
  it("yields parsed slot events and skips malformed frames", async () => {
    const client = new FeedClient("https://relay.test");
    const ac = new AbortController();
    const gen = client.streamSlotEvents(FEED, KEY, {
      getSince: () => 0,
      signal: ac.signal,
    });

    const first = gen.next();
    await Promise.resolve();
    const es = FakeEventSource.last();
    expect(es.url).toContain(`/feeds/${FEED}/stream`);
    expect(es.url).toContain("since=0");
    expect(es.url).toContain("auth=");

    es.open();
    es.emitRaw("{not json"); // ignored
    es.emit({ slot_id: "a", created_at: 3 });
    const r1 = await first;
    expect(r1.value).toEqual({ slot_id: "a", created_at: 3 });

    const second = gen.next();
    es.emit({ slot_id: "b", created_at: 4 });
    const r2 = await second;
    expect(r2.value).toEqual({ slot_id: "b", created_at: 4 });

    ac.abort();
    const end = await gen.next();
    expect(end.done).toBe(true);
    expect(es.closed).toBe(true);
  });

  it("reconnects with a fresh url at the updated cursor after a drop", async () => {
    vi.useFakeTimers();
    try {
      const client = new FeedClient("https://relay.test");
      const ac = new AbortController();
      let cursor = 0;
      const gen = client.streamSlotEvents(FEED, KEY, {
        getSince: () => cursor,
        signal: ac.signal,
      });

      const first = gen.next();
      await vi.advanceTimersByTimeAsync(0);
      const es0 = FakeEventSource.last();
      expect(es0.url).toContain("since=0");
      es0.open();
      es0.emit({ slot_id: "a", created_at: 7 });
      expect((await first).value).toEqual({ slot_id: "a", created_at: 7 });

      cursor = 7; // caller advanced its cursor after processing "a"

      const second = gen.next();
      await vi.advanceTimersByTimeAsync(0);
      es0.error(); // connection dropped (or the server's ~4min cap)
      await vi.advanceTimersByTimeAsync(1000); // STREAM_RECONNECT_MS backoff

      const es1 = FakeEventSource.last();
      expect(es1).not.toBe(es0);
      expect(es0.closed).toBe(true);
      expect(es1.url).toContain("since=7"); // resumes from the new cursor

      es1.open();
      es1.emit({ slot_id: "b", created_at: 8 });
      expect((await second).value).toEqual({ slot_id: "b", created_at: 8 });

      ac.abort();
      await gen.next();
    } finally {
      vi.useRealTimers();
    }
  });

  it("throws StreamUnsupportedError if the stream never opens", async () => {
    vi.useFakeTimers();
    try {
      const client = new FeedClient("https://relay.test");
      const ac = new AbortController();
      const gen = client.streamSlotEvents(FEED, KEY, {
        getSince: () => 0,
        signal: ac.signal,
      });
      const result = gen.next();
      // Attach the rejection handler up front: the throw lands mid-loop, and an
      // unhandled rejection (even momentarily) would fail the run.
      const assertion = expect(result).rejects.toBeInstanceOf(StreamUnsupportedError);
      // Three error-before-open cycles → give up (STREAM_UNSUPPORTED_RETRIES).
      for (let i = 0; i < 3; i++) {
        await vi.advanceTimersByTimeAsync(0);
        FakeEventSource.last().error();
        await vi.advanceTimersByTimeAsync(1000);
      }
      await assertion;
    } finally {
      vi.useRealTimers();
    }
  });
});
