// Framework-free feed actions: open (mint), join, leave, and the roster refresh
// with TOFU prompts. Mirror nomnom.py cmd_open / cmd_join / _refresh_roster_with_tofu.
// The UI layer (hooks) wires these to the store; nothing here touches React.

import { FeedClient, mintFeed } from "../relay/feed-client";
import { feedKeyFromToken } from "../crypto/feeds";
import { ikFingerprint } from "../crypto/fingerprint";
import { bytesToHexDigest } from "../crypto/hex";
import { randomHex } from "../util/ids";
import { formatFeedUrl, parseFeedUrl } from "../util/feed-url";
import type { Feed, Identity, Member, OnTofu, RelayConfig } from "../types";

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
  client: FeedClient;
}

export function feedContext(feed: Feed, identity: Identity): FeedContext {
  const feedKey = feedKeyFromToken(feed.feed_token);
  const origin = new URL(feed.url).origin;
  return {
    feed,
    identity,
    feedKey,
    feedKeyHex: bytesToHexDigest(feedKey),
    client: new FeedClient(origin),
  };
}

function memberCard(identity: Identity, memberId: string): Member {
  return { member_id: memberId, identity_pubkey: identity.sig_pub, name: identity.name };
}

/**
 * TOFU one member: prompt (or auto-trust) and pin on accept. Skips self,
 * identities already in `known`, and already-pinned ones; every identity it
 * considers is added to `known` so a later sighting never re-prompts.
 * Advisory — it pins on accept but never blocks; validly-signed posts are
 * always delivered, matching the CLI.
 */
export async function tofuMember(
  m: Member,
  ctx: FeedContext,
  hooks: TofuHooks,
  known: Set<string>,
): Promise<void> {
  if (!m || m.member_id === ctx.feed.member_id) return;
  const sigPub = m.identity_pubkey;
  if (!sigPub || known.has(sigPub)) return;
  known.add(sigPub);
  if (hooks.isPinned(sigPub)) return;
  const name = m.name || "(no name)";
  const ok = hooks.trustNew
    ? true
    : await hooks.onTofu({ peerName: name, sigPub, fingerprint: ikFingerprint(sigPub) });
  if (ok) hooks.pinPeer(sigPub, name);
}

/** Identities the feed's cached roster already vouched for. */
export function knownIdentities(feed: Feed): Set<string> {
  return new Set((feed.members_cache ?? []).map((m) => m.identity_pubkey));
}

/**
 * Fetch the live roster and prompt TOFU on any newly-seen identities. `known`
 * defaults to the cached roster; a long-lived caller passes its own set so
 * later member frames share it.
 */
export async function refreshRoster(
  ctx: FeedContext,
  hooks: TofuHooks,
  signal?: AbortSignal,
  known: Set<string> = knownIdentities(ctx.feed),
): Promise<Member[]> {
  const roster = await ctx.client.listMembers(ctx.feed.feed_id, ctx.feedKey, { signal });
  for (const m of roster) await tofuMember(m, ctx, hooks, known);
  return roster;
}

export interface OpenFeedParams {
  identity: Identity;
  relay: RelayConfig;
  name: string;
  signal?: AbortSignal;
}

/** Mint a new feed on the configured relay and return the local Feed record. */
export async function openFeed(p: OpenFeedParams): Promise<Feed> {
  const memberId = randomHex(16);
  const result = await mintFeed(p.relay, memberCard(p.identity, memberId), p.signal);
  const feedId = result.feed_id;
  if (!feedId) throw new Error("relay did not return a feed_id");
  const host = new URL(p.relay.url).origin;
  const created = result.created_at || Math.floor(Date.now() / 1000);
  return {
    name: p.name,
    feed_id: feedId,
    feed_token: feedId,
    url: formatFeedUrl(host, feedId),
    joined_at: created,
    member_id: memberId,
    members_cache: [
      { ...memberCard(p.identity, memberId), joined_at: created },
    ],
    last_seq: 0,
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
  await client.getMeta(feedId, feedKey, p.signal); // 404s here if the URL is wrong
  const memberId = randomHex(16);
  await client.putMember(feedId, feedKey, memberId, memberCard(p.identity, memberId), p.signal);
  const roster = await client.listMembers(feedId, feedKey, { signal: p.signal });

  const feed: Feed = {
    name: p.name,
    feed_id: feedId,
    feed_token: feedId,
    url: formatFeedUrl(host, feedId),
    joined_at: Math.floor(Date.now() / 1000),
    member_id: memberId,
    members_cache: roster,
    last_seq: 0,
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
