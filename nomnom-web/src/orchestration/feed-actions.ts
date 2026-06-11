// Framework-free feed actions: open (mint), join, leave, and the roster refresh
// with TOFU prompts. Mirror nomnom.py cmd_open / cmd_join / _refresh_roster_with_tofu.
// The UI layer (hooks) wires these to the store; nothing here touches React.

import { FeedClient, mintFeed, type MemberCard } from "../relay/feed-client";
import { feedKeyFromToken } from "../crypto/feeds";
import { ikFingerprint } from "../crypto/fingerprint";
import { bytesToHexDigest } from "../crypto/hex";
import { randomHex } from "../util/ids";
import { formatFeedUrl, parseFeedUrl } from "../util/feed-url";
import type { Feed, Identity, Member, OnTofu, RelayConfig } from "../types";

const DEFAULT_TTL_SECONDS = 86_400; // 1 day, matches the CLI default

export interface TofuHooks {
  isPinned: (sigPub: string) => boolean;
  onTofu: OnTofu;
  pinPeer: (sigPub: string, name: string) => void;
  trustNew?: boolean;
}

/** A live feed key + signed-request client, derived from a stored Feed. */
export interface FeedContext {
  feed: Feed;
  identity: Identity;
  feedKey: Uint8Array;
  feedKeyHex: string;
  host: string;
  client: FeedClient;
}

export function feedContext(feed: Feed, identity: Identity): FeedContext {
  const feedKey = feedKeyFromToken(feed.feed_token);
  return {
    feed,
    identity,
    feedKey,
    feedKeyHex: bytesToHexDigest(feedKey),
    host: new URL(feed.url).origin,
    client: new FeedClient(new URL(feed.url).origin),
  };
}

function memberCard(identity: Identity, memberId: string): MemberCard {
  return { member_id: memberId, identity_pubkey: identity.sig_pub, name: identity.name };
}

/**
 * Fetch the live roster and prompt TOFU on any newly-seen identities (skipping
 * self, identities already in the feed's cache, and already-pinned ones). TOFU
 * here is advisory — it pins on accept but never blocks; validly-signed posts
 * are always delivered, matching the CLI.
 */
export async function refreshRoster(
  ctx: FeedContext,
  hooks: TofuHooks,
  signal?: AbortSignal,
): Promise<Member[]> {
  const roster = await ctx.client.listMembers(ctx.feed.feed_id, ctx.feedKey, { signal });
  const knownInCache = new Set((ctx.feed.members_cache ?? []).map((m) => m.identity_pubkey));
  for (const m of roster) {
    if (!m || m.member_id === ctx.feed.member_id) continue;
    const sigPub = m.identity_pubkey;
    if (!sigPub || knownInCache.has(sigPub) || hooks.isPinned(sigPub)) continue;
    const name = m.name || "(no name)";
    const ok = hooks.trustNew
      ? true
      : await hooks.onTofu({ peerName: name, sigPub, fingerprint: ikFingerprint(sigPub) });
    if (ok) hooks.pinPeer(sigPub, name);
  }
  return roster;
}

export interface OpenFeedParams {
  identity: Identity;
  relay: RelayConfig;
  name: string;
  ttlSeconds?: number;
  signal?: AbortSignal;
}

/** Mint a new feed on the configured relay and return the local Feed record. */
export async function openFeed(p: OpenFeedParams): Promise<Feed> {
  const memberId = randomHex(16);
  const result = await mintFeed(
    p.relay,
    p.ttlSeconds ?? DEFAULT_TTL_SECONDS,
    memberCard(p.identity, memberId),
    p.signal,
  );
  const feedId = result.feed_id;
  if (!feedId) throw new Error("relay did not return a feed_id");
  const host = new URL(p.relay.url).origin;
  const created = result.created_at || Math.floor(Date.now() / 1000);
  return {
    name: p.name,
    feed_id: feedId,
    feed_token: feedId,
    url: formatFeedUrl(host, feedId),
    expires_at: result.expires_at || created + (p.ttlSeconds ?? DEFAULT_TTL_SECONDS),
    joined_at: created,
    member_id: memberId,
    members_cache: [
      { ...memberCard(p.identity, memberId), joined_at: created },
    ],
    last_post_ts: 0,
    auto_save: false,
  };
}

export interface JoinFeedParams {
  identity: Identity;
  url: string;
  name: string;
  hooks: TofuHooks;
  signal?: AbortSignal;
}

/** Join an existing feed by URL: probe, publish a member card, fetch the roster. */
export async function joinFeed(p: JoinFeedParams): Promise<Feed> {
  const { host, feedId } = parseFeedUrl(p.url);
  const feedKey = feedKeyFromToken(feedId);
  const client = new FeedClient(host);
  const meta = await client.getMeta(feedId, feedKey, p.signal); // 404s here if the URL is wrong
  const memberId = randomHex(16);
  await client.putMember(feedId, feedKey, memberId, memberCard(p.identity, memberId), p.signal);
  const roster = await client.listMembers(feedId, feedKey, { signal: p.signal });

  const feed: Feed = {
    name: p.name,
    feed_id: feedId,
    feed_token: feedId,
    url: formatFeedUrl(host, feedId),
    expires_at: meta.expires_at || 0,
    joined_at: Math.floor(Date.now() / 1000),
    member_id: memberId,
    members_cache: roster,
    last_post_ts: 0,
    auto_save: false,
  };
  // Prompt TOFU on the members already present.
  await refreshRoster(feedContext(feed, p.identity), p.hooks, p.signal);
  return feed;
}

/** Leave a feed: best-effort member-card deletion on the relay. */
export async function leaveFeed(feed: Feed, identity: Identity, signal?: AbortSignal): Promise<void> {
  const ctx = feedContext(feed, identity);
  await ctx.client.deleteMember(feed.feed_id, ctx.feedKey, feed.member_id, signal);
}
