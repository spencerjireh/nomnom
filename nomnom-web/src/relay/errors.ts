// Relay error mapping. Mirrors nomnom.py's handling: GET 404 means "nothing in
// the slot yet" (returned as null, not thrown); 409/410/413/401 are hard errors.

export class RelayError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string,
  ) {
    super(`relay ${status}: ${reason}`);
    this.name = "RelayError";
  }
}

/** Thrown by the SSE stream when /stream never opens — the relay predates the
 * push endpoint. The caller falls back to the long-poll loop. */
export class StreamUnsupportedError extends Error {
  constructor() {
    super("relay does not support the SSE /stream endpoint");
    this.name = "StreamUnsupportedError";
  }
}

/** A friendlier message for the few statuses a user can actually act on. */
export function friendlyRelayMessage(e: unknown): string {
  if (!(e instanceof RelayError)) return e instanceof Error ? e.message : String(e);
  switch (e.status) {
    case 401:
      if (e.reason === "clock-skew") {
        return "relay rejected the request: your system clock is off by more than 5 minutes.";
      }
      return "relay rejected the request (bad passphrase?).";
    case 409:
      return "slot busy — a transfer to this peer is already in flight. retry shortly.";
    case 410:
      return "the slot expired before it was read (5 min TTL). retry.";
    case 413:
      return "payload too large for the relay.";
    default:
      return e.message;
  }
}
