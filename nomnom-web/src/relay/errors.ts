// Relay error mapping. Mirrors nomnom.py's handling: GET 404 on a slot means
// "not on the relay" (returned as null, not thrown); 409/413/401 are hard errors.

export class RelayError extends Error {
  constructor(
    readonly status: number,
    readonly reason: string,
  ) {
    super(`relay ${status}: ${reason}`);
    this.name = "RelayError";
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
      // The relay's `error` reason is the stable vocabulary; status is the fallback.
      if (e.reason === "feed-full") return "the channel is full (64 devices).";
      return "a post with that id already exists on the relay. retry.";
    case 413:
      return "payload too large for the relay.";
    default:
      return e.message;
  }
}
