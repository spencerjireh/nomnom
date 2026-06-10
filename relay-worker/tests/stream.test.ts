import { SELF } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { feedStreamUrl, mintFeed, signedFeedRequest } from "./helpers";

interface StreamResult {
  status: number;
  events: { slot_id: string; created_at: number }[];
}

// Open a /stream URL and read SSE `data:` frames until we have `want` of them or
// the budget elapses. Each read races a deadline; on timeout we simply break and
// leave the final read pending — a pending promise is harmless, whereas
// cancelling/aborting the workerd-proxied stream surfaces an unhandled rejection.
// The worker test pool tears down the isolate (and the DO's stream) at run end.
// Comment frames (`: ok` / `: ping`) are skipped.
async function streamEvents(
  url: string,
  want: number,
  budgetMs = 4000,
): Promise<StreamResult> {
  const res = await SELF.fetch(url);
  if (res.status !== 200 || res.body === null) {
    return { status: res.status, events: [] };
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  const events: { slot_id: string; created_at: number }[] = [];
  let buf = "";
  const deadline = Date.now() + budgetMs;
  while (events.length < want) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) break;
    const chunk = await Promise.race([
      reader.read(),
      new Promise<null>((r) => setTimeout(() => r(null), remaining)),
    ]);
    if (chunk === null || chunk.done) break; // timed out (read left pending) / EOF
    buf += dec.decode(chunk.value, { stream: true });
    let nl: number;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl);
      buf = buf.slice(nl + 1);
      if (line.startsWith("data:")) events.push(JSON.parse(line.slice(5).trim()));
    }
  }
  return { status: 200, events };
}

async function putSlot(feedId: string, slotId: string): Promise<void> {
  const body = new TextEncoder().encode("payload");
  const req = await signedFeedRequest(
    "PUT",
    `/feeds/${feedId}/slots/${slotId}`,
    feedId,
    { body, contentLength: body.byteLength },
  );
  expect((await SELF.fetch(req)).status).toBe(204);
}

describe("GET /feeds/:id/stream (SSE)", () => {
  it("sets the event-stream content type", async () => {
    const m = await mintFeed(SELF);
    const ctrl = new AbortController();
    const res = await SELF.fetch(await feedStreamUrl(m.feed_id, 0), {
      signal: ctrl.signal,
    });
    expect(res.status).toBe(200);
    expect(res.headers.get("Content-Type")).toContain("text/event-stream");
    ctrl.abort();
  });

  it("replays the backlog of slots since the cursor on connect", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "back-1");
    await putSlot(m.feed_id, "back-2");

    const { status, events } = await streamEvents(
      await feedStreamUrl(m.feed_id, 0),
      2,
    );
    expect(status).toBe(200);
    expect(events.map((e) => e.slot_id).sort()).toEqual(["back-1", "back-2"]);
  });

  it("respects the since cursor (no backlog before it)", async () => {
    const m = await mintFeed(SELF);
    await putSlot(m.feed_id, "old");
    const future = Math.floor(Date.now() / 1000) + 60;

    // since is in the future → nothing replayed within the short budget.
    const { events } = await streamEvents(
      await feedStreamUrl(m.feed_id, future),
      1,
      1200,
    );
    expect(events).toEqual([]);
  });

  it("pushes a slot posted after connect (live notify)", async () => {
    const m = await mintFeed(SELF);
    const sinceNow = Math.floor(Date.now() / 1000);
    const url = await feedStreamUrl(m.feed_id, sinceNow);

    // Post shortly after the stream opens; the DO should push it live.
    const posted = (async () => {
      await new Promise((r) => setTimeout(r, 200));
      await putSlot(m.feed_id, "live-1");
    })();

    const { events } = await streamEvents(url, 1);
    await posted;
    expect(events.map((e) => e.slot_id)).toContain("live-1");
  });

  it("accepts query-param auth but rejects a bad MAC", async () => {
    const m = await mintFeed(SELF);
    const bad = await feedStreamUrl(m.feed_id, 0, {
      macOverride: "deadbeef".repeat(8),
    });
    expect((await SELF.fetch(bad)).status).toBe(401);
  });

  it("rejects an out-of-window (clock-skew) timestamp", async () => {
    const m = await mintFeed(SELF);
    const skewed = await feedStreamUrl(m.feed_id, 0, {
      tsOverride: Math.floor(Date.now() / 1000) - 1000,
    });
    const res = await SELF.fetch(skewed);
    expect(res.status).toBe(401);
    expect(await res.text()).toContain("clock-skew");
  });
});
