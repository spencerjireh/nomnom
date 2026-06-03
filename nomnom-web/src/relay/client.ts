// Minimal relay client for the unauthenticated health probe (used by onboarding
// and settings). Feed traffic goes through FeedClient (feed-client.ts); feed
// minting through mintFeed there.

import type { RelayConfig } from "../types";

export class RelayClient {
  constructor(private readonly config: RelayConfig) {}

  private url(path: string): string {
    return this.config.url.replace(/\/+$/, "") + path;
  }

  /** Connectivity probe (no auth). */
  async health(signal?: AbortSignal): Promise<boolean> {
    try {
      const res = await fetch(this.url("/health"), { method: "GET", signal });
      return res.ok;
    } catch {
      return false;
    }
  }
}
