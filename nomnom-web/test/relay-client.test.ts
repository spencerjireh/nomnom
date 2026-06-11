// Unit tests for RelayClient.verifyAuth — the authenticated passphrase probe that
// replaced the unauthenticated /health check in Settings. `fetch` is mocked so we
// can drive the relay's 401/404 responses and assert the request is HMAC-signed.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RelayClient } from "../src/relay/client";

const RELAY = { url: "https://relay.example.com", secret: "fetal-crawl-wing-heave-broad-thus" };

function jsonResponse(status: number, body: unknown): Response {
  return { status, json: async () => body } as unknown as Response;
}

function client(): RelayClient {
  return new RelayClient(RELAY);
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  // crypto.randomUUID exists in node 18+, but stub for determinism of the path.
  vi.stubGlobal("crypto", { ...globalThis.crypto, randomUUID: () => "00000000-test" });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("RelayClient.verifyAuth", () => {
  it("maps 401 bad-mac to 'rejected'", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { error: "bad-mac" }));
    expect(await client().verifyAuth()).toBe("rejected");
  });

  it("maps 401 clock-skew to 'skew'", async () => {
    fetchMock.mockResolvedValue(jsonResponse(401, { error: "clock-skew" }));
    expect(await client().verifyAuth()).toBe("skew");
  });

  it("treats 404 (missing slot) as 'ok' — the passphrase signed fine", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { error: "not-found" }));
    expect(await client().verifyAuth()).toBe("ok");
  });

  it("fails closed on an unexpected status (e.g. 500) — not 'ok'", async () => {
    fetchMock.mockResolvedValue(jsonResponse(500, { error: "relay-misconfigured" }));
    expect(await client().verifyAuth()).toBe("unreachable");
  });

  it("maps a network/CORS failure to 'unreachable'", async () => {
    fetchMock.mockRejectedValue(new TypeError("Failed to fetch"));
    expect(await client().verifyAuth()).toBe("unreachable");
  });

  it("falls back to 'rejected' on a non-JSON 401 body", async () => {
    fetchMock.mockResolvedValue({
      status: 401,
      json: async () => {
        throw new SyntaxError("Unexpected token");
      },
    } as unknown as Response);
    expect(await client().verifyAuth()).toBe("rejected");
  });

  it("signs the probe with an HMAC Authorization header against /slots/", async () => {
    fetchMock.mockResolvedValue(jsonResponse(404, { error: "not-found" }));
    await client().verifyAuth();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("https://relay.example.com/slots/nomnom-authcheck-00000000-test");
    expect(init.method).toBe("GET");
    expect(init.headers.Authorization).toMatch(/^NMNM-HMAC-SHA256 \d+:[0-9a-f]{64}$/);
  });
});
