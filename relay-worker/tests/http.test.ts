import { describe, expect, it } from "vitest";
import { MAX_BODY_BYTES, rejectBody } from "../src/http";

// A streamed request body carries no automatic Content-Length, which lets us
// exercise the "missing/forged Content-Length" paths that a fixed body (which
// the Request constructor auto-labels) can't reach.
function streamedRequest(headers: Record<string, string>): Request {
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new Uint8Array([1]));
      c.close();
    },
  });
  return new Request("https://relay.test/feeds/f/slots/s", {
    method: "PUT",
    headers,
    body,
    // Required by workerd when the body is a stream.
    duplex: "half",
  } as RequestInit & { duplex: "half" });
}

describe("rejectBody", () => {
  it("411 when Content-Length is absent (chunked / unknown length)", () => {
    const res = rejectBody(streamedRequest({}), MAX_BODY_BYTES);
    expect(res?.status).toBe(411);
  });

  it("411 when Content-Length is unparseable", () => {
    const res = rejectBody(
      streamedRequest({ "Content-Length": "not-a-number" }),
      MAX_BODY_BYTES,
    );
    expect(res?.status).toBe(411);
  });

  it("411 when Content-Length is negative", () => {
    const res = rejectBody(
      streamedRequest({ "Content-Length": "-5" }),
      MAX_BODY_BYTES,
    );
    expect(res?.status).toBe(411);
  });

  it("413 when Content-Length exceeds the cap", () => {
    const res = rejectBody(
      streamedRequest({ "Content-Length": String(MAX_BODY_BYTES + 1) }),
      MAX_BODY_BYTES,
    );
    expect(res?.status).toBe(413);
  });

  it("passes (null) a well-formed within-cap body", () => {
    const res = rejectBody(
      streamedRequest({ "Content-Length": "10" }),
      MAX_BODY_BYTES,
    );
    expect(res).toBeNull();
  });

  it("400 when Content-Length is valid but there is no body", () => {
    const req = new Request("https://relay.test/feeds/f/slots/s", {
      method: "PUT",
      headers: { "Content-Length": "0" },
    });
    const res = rejectBody(req, MAX_BODY_BYTES);
    expect(res?.status).toBe(400);
  });
});
