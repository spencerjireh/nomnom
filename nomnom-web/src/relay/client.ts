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
   * Probes `GET /auth`, a side-effect-free HMAC-gated endpoint that answers 204
   * to a valid signature. A bad secret yields 401 `bad-mac`/`missing-*`; a stale
   * clock yields 401 `clock-skew`.
   */
  async verifyAuth(signal?: AbortSignal): Promise<AuthCheck> {
    const path = "/auth";
    try {
      const res = await fetch(this.url(path), {
        method: "GET",
        headers: { Authorization: relayAuthHeader(this.config.secret, "GET", path) },
        signal,
      });
      // Fail closed: only 204 means "ok"; 401 distinguishes skew vs reject;
      // anything else (5xx, a relay without /auth, a stripped proxy response)
      // is unexpected, so don't claim the passphrase is valid.
      if (res.status === 204) return "ok";
      if (res.status === 401) {
        let reason = "";
        try {
          reason = ((await res.json()) as { error?: string })?.error ?? "";
        } catch {
          // non-JSON 401 body — treat as a plain rejection
        }
        return reason === "clock-skew" ? "skew" : "rejected";
      }
      return "unreachable";
    } catch {
      return "unreachable";
    }
  }
}
