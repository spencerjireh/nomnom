// Minimal relay client for the unauthenticated health probe and the
// authenticated passphrase check (used by onboarding and settings). Feed traffic
// goes through FeedClient (feed-client.ts); feed minting through mintFeed there.

import type { RelayConfig } from "../types";
import { relayAuthHeader } from "../crypto/relay-auth";

/** Result of an authenticated passphrase probe — see `verifyAuth`. */
export type AuthCheck = "ok" | "rejected" | "skew" | "unreachable";

export class RelayClient {
  constructor(private readonly config: RelayConfig) {}

  private url(path: string): string {
    return this.config.url.replace(/\/+$/, "") + path;
  }

  /** Connectivity probe (no auth). Proves the URL is reachable, nothing more. */
  async health(signal?: AbortSignal): Promise<boolean> {
    try {
      const res = await fetch(this.url("/health"), { method: "GET", signal });
      return res.ok;
    } catch {
      return false;
    }
  }

  /**
   * Validate the relay HMAC passphrase by actually signing a request, instead of
   * just pinging /health (which takes no auth and so can't catch a wrong secret).
   *
   * Probes a side-effect-free HMAC-gated endpoint: `GET /slots/<random>`. A bad
   * secret yields 401 `bad-mac`/`missing-*`; a stale clock yields 401 `clock-skew`;
   * a good secret hits the missing slot and returns 404 (delete-on-read only fires
   * on a *found* slot, and without `?wait` the relay answers immediately). The
   * random reserved id can never collide with — or consume — a real slot.
   */
  async verifyAuth(signal?: AbortSignal): Promise<AuthCheck> {
    const path = `/slots/nomnom-authcheck-${crypto.randomUUID()}`;
    try {
      const res = await fetch(this.url(path), {
        method: "GET",
        headers: { Authorization: relayAuthHeader(this.config.secret, "GET", path) },
        signal,
      });
      if (res.status !== 401) return "ok";
      let reason = "";
      try {
        reason = ((await res.json()) as { error?: string })?.error ?? "";
      } catch {
        // non-JSON 401 body — treat as a plain rejection
      }
      return reason === "clock-skew" ? "skew" : "rejected";
    } catch {
      return "unreachable";
    }
  }
}
