// Unit tests for FeedClient.watch — the WebSocket frame generator — and the
// parseFrame guard. Node ships a live global WebSocket, so a controllable fake
// is stubbed in for each test.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FeedClient, parseFrame } from "../src/relay/feed-client";
import { WS_PING_INTERVAL_MS, WS_RECONNECT_MIN_MS } from "../src/config";

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  static last(): FakeWebSocket {
    return FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  }
  static readonly OPEN = 1;
  readonly OPEN = 1;
  readyState = 0;
  closed = false;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;
  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }
  send(d: string): void {
    this.sent.push(d);
  }
  close(): void {
    this.closed = true;
    this.readyState = 3;
  }
  // test drivers
  open(): void {
    this.readyState = 1;
    this.onopen?.();
  }
  emit(obj: unknown): void {
    this.onmessage?.({ data: JSON.stringify(obj) });
  }
  emitRaw(s: string): void {
    this.onmessage?.({ data: s });
  }
  drop(): void {
    this.readyState = 3;
    this.onclose?.();
  }
}

const KEY = new Uint8Array(32);
const FEED = "feedtoken01";

beforeEach(() => {
  FakeWebSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeWebSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("parseFrame", () => {
  it("accepts the three frame types", () => {
    expect(parseFrame(JSON.stringify({ type: "post", seq: 3, slot_id: "s", created_at: 9 }))).toEqual({
      type: "post",
      seq: 3,
      slot_id: "s",
      created_at: 9,
    });
    expect(
      parseFrame(
        JSON.stringify({
          type: "member",
          action: "join",
          member: { member_id: "m", identity_pubkey: "p", name: "n", joined_at: 1 },
        }),
      ),
    ).toEqual({
      type: "member",
      action: "join",
      member: { member_id: "m", identity_pubkey: "p", name: "n", joined_at: 1 },
    });
    expect(parseFrame(JSON.stringify({ type: "deleted", slot_id: "s" }))).toEqual({
      type: "deleted",
      slot_id: "s",
    });
  });

  it("rejects pong, non-JSON, unknown types, and malformed frames", () => {
    expect(parseFrame("pong")).toBeNull();
    expect(parseFrame("{not json")).toBeNull();
    expect(parseFrame(JSON.stringify({ type: "nope" }))).toBeNull();
    expect(parseFrame(JSON.stringify({ type: "post", slot_id: "s" }))).toBeNull();
    expect(parseFrame(JSON.stringify({ type: "member", action: "join" }))).toBeNull();
    expect(parseFrame(JSON.stringify({ type: "member", action: "kick", member: {} }))).toBeNull();
    expect(parseFrame(new ArrayBuffer(2))).toBeNull();
  });
});

describe("FeedClient.watch", () => {
  it("opens a signed wss URL and yields parsed frames, skipping junk", async () => {
    const client = new FeedClient("https://relay.test");
    const ac = new AbortController();
    const gen = client.watch(FEED, KEY, { getSince: () => 0, signal: ac.signal });

    const first = gen.next();
    await Promise.resolve();
    const ws = FakeWebSocket.last();
    expect(ws.url).toMatch(/^wss:\/\/relay\.test\/feeds\/feedtoken01\/ws\?since=0&auth=\d+:[0-9a-f]{64}$/);

    ws.open();
    ws.emitRaw("pong");
    ws.emitRaw("garbage");
    ws.emit({ type: "post", seq: 1, slot_id: "a", created_at: 10 });
    expect((await first).value).toEqual({ type: "post", seq: 1, slot_id: "a", created_at: 10 });

    ws.emit({ type: "deleted", slot_id: "a" });
    expect((await gen.next()).value).toEqual({ type: "deleted", slot_id: "a" });

    ac.abort();
    await gen.return(undefined);
    expect(ws.closed).toBe(true);
  });

  it("reconnects after a drop with the updated cursor and a fresh signature", async () => {
    vi.useFakeTimers();
    const client = new FeedClient("http://localhost:8787");
    const ac = new AbortController();
    let since = 0;
    const gen = client.watch(FEED, KEY, { getSince: () => since, signal: ac.signal });

    const first = gen.next();
    await vi.advanceTimersByTimeAsync(0);
    const ws1 = FakeWebSocket.last();
    expect(ws1.url.startsWith("ws://localhost:8787/")).toBe(true);
    ws1.open();
    ws1.emit({ type: "post", seq: 5, slot_id: "a", created_at: 10 });
    expect((await first).value).toMatchObject({ seq: 5 });
    since = 5;

    const second = gen.next();
    await vi.advanceTimersByTimeAsync(0);
    ws1.drop();
    // Backoff before the reconnect; nothing yet.
    await vi.advanceTimersByTimeAsync(WS_RECONNECT_MIN_MS - 1);
    expect(FakeWebSocket.instances).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    const ws2 = FakeWebSocket.last();
    expect(ws2.url).toContain("since=5");
    expect(ws2.url.split("auth=")[1]).not.toBe(ws1.url.split("auth=")[1]);

    ws2.open();
    ws2.emit({ type: "post", seq: 6, slot_id: "b", created_at: 11 });
    expect((await second).value).toMatchObject({ seq: 6 });

    ac.abort();
    await gen.return(undefined);
  });

  it("doubles the backoff across consecutive failures and resets on open", async () => {
    vi.useFakeTimers();
    const client = new FeedClient("https://relay.test");
    const ac = new AbortController();
    const gen = client.watch(FEED, KEY, { getSince: () => 0, signal: ac.signal });

    const pending = gen.next();
    await vi.advanceTimersByTimeAsync(0);
    FakeWebSocket.last().drop(); // never opened → 1s
    await vi.advanceTimersByTimeAsync(WS_RECONNECT_MIN_MS);
    expect(FakeWebSocket.instances).toHaveLength(2);
    FakeWebSocket.last().drop(); // → 2s
    await vi.advanceTimersByTimeAsync(WS_RECONNECT_MIN_MS * 2 - 1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeWebSocket.instances).toHaveLength(3);
    FakeWebSocket.last().drop(); // → 4s
    await vi.advanceTimersByTimeAsync(WS_RECONNECT_MIN_MS * 4 - 1);
    expect(FakeWebSocket.instances).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(1);
    expect(FakeWebSocket.instances).toHaveLength(4);

    // An open resets the backoff to the minimum.
    FakeWebSocket.last().open();
    FakeWebSocket.last().drop();
    await vi.advanceTimersByTimeAsync(WS_RECONNECT_MIN_MS);
    expect(FakeWebSocket.instances).toHaveLength(5);

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(pending).resolves.toMatchObject({ done: true });
  });

  it("sends a ping every WS_PING_INTERVAL_MS while open", async () => {
    vi.useFakeTimers();
    const client = new FeedClient("https://relay.test");
    const ac = new AbortController();
    const gen = client.watch(FEED, KEY, { getSince: () => 0, signal: ac.signal });

    const pending = gen.next();
    await vi.advanceTimersByTimeAsync(0);
    const ws = FakeWebSocket.last();
    ws.open();
    await vi.advanceTimersByTimeAsync(WS_PING_INTERVAL_MS);
    expect(ws.sent).toEqual(["ping"]);
    await vi.advanceTimersByTimeAsync(WS_PING_INTERVAL_MS);
    expect(ws.sent).toEqual(["ping", "ping"]);

    ac.abort();
    await vi.advanceTimersByTimeAsync(0);
    await expect(pending).resolves.toMatchObject({ done: true });
    // The ping timer is torn down with the socket.
    await vi.advanceTimersByTimeAsync(WS_PING_INTERVAL_MS);
    expect(ws.sent).toHaveLength(2);
    expect(ws.closed).toBe(true);
  });

  it("aborting closes the socket and ends the generator", async () => {
    const client = new FeedClient("https://relay.test");
    const ac = new AbortController();
    const gen = client.watch(FEED, KEY, { getSince: () => 0, signal: ac.signal });

    const pending = gen.next();
    await Promise.resolve();
    const ws = FakeWebSocket.last();
    ws.open();
    ac.abort();
    await expect(pending).resolves.toMatchObject({ done: true });
    expect(ws.closed).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
