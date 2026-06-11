// Feed-URL formatting + strict parsing (https://<host>/f/<token>). Pure string
// logic, kept out of the orchestration layer so it stays a focused, testable unit.

import { FEED_TOKEN_RE } from "../crypto/constants";

export function formatFeedUrl(host: string, feedId: string): string {
  return `${host.replace(/\/+$/, "")}/f/${feedId}`;
}

/** Split a feed URL into (origin, feedId). Strict: requires /f/<token>. */
export function parseFeedUrl(raw: string): { host: string; feedId: string } {
  const u = raw.trim();
  if (!u) throw new Error("feed url must not be empty");
  const withScheme = /^https?:\/\//.test(u) ? u : `https://${u}`;
  const m = withScheme.match(/^(https?:\/\/[^/]+)\/f\/([^/?#]+)$/);
  if (!m) throw new Error("feed url must look like https://<host>/f/<token>");
  const feedId = m[2];
  if (!FEED_TOKEN_RE.test(feedId)) {
    throw new Error("feed token in url must be 8-32 url-safe base64 chars");
  }
  return { host: m[1], feedId };
}
