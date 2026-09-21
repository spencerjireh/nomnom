// RECEIVE = watch a feed for new posts. The relay pushes typed frames over a
// WebSocket (post / member / deleted). For each post, GET it, feed_open
// (verifies signature + content hash), skip our own posts, then hand the file
// to onFile. Member frames keep the roster fresh (TOFU on first sight);
// deleted frames drop rows. Runs until the signal aborts. Mirrors nomnom.py
// cmd_receive, which long-polls the same index instead.

import { cryptoClient } from "../worker/cryptoClient";
import {
  feedContext,
  knownIdentities,
  refreshRoster,
  tofuMember,
  type FeedContext,
  type TofuHooks,
} from "./feed-actions";
import { WS_RECONNECT_MIN_MS } from "../config";
import { sleep } from "../util/sleep";
import type { FeedHeader } from "../crypto/feeds";
import type { FeedFrame } from "../relay/feed-client";
import type { Feed, Identity, Member } from "../types";

export interface ReceivedFile {
  name: string;
  body: ArrayBuffer;
  bytes: number;
  peerName: string;
  slot_id: string;
}

export interface ReceiveParams {
  feed: Feed;
  identity: Identity;
  hooks: TofuHooks;
  onFile: (f: ReceivedFile) => void;
  /** Persist forward progress (last_seq). */
  onAdvance: (lastSeq: number) => void;
  onRoster?: (roster: Member[]) => void;
  /** A post was hard-deleted on the relay (by any device). */
  onDeleted?: (slotId: string) => void;
  signal: AbortSignal;
  /** Test seam: pre-built context (key + client). Defaults to feedContext(feed, identity). */
  ctx?: FeedContext;
}

/** Watch until aborted; returns the number of files received. */
export async function runReceive(p: ReceiveParams): Promise<number> {
  const ctx = p.ctx ?? feedContext(p.feed, p.identity);
  let lastSeq = p.feed.last_seq;
  let roster: Member[] = p.feed.members_cache ?? [];
  let count = 0;

  const advance = (seq: number) => {
    if (seq > lastSeq) {
      lastSeq = seq;
      p.onAdvance(lastSeq);
    }
  };

  // Identities TOFU has already considered this session (seeded from the
  // cache), so a member frame for a known device never re-prompts.
  const known = knownIdentities(p.feed);

  // One roster fetch up front so TOFU runs on members already present before
  // any post arrives. Non-fatal: the socket's member frames take over from here.
  try {
    roster = await refreshRoster(ctx, p.hooks, p.signal, known);
    p.onRoster?.(roster);
  } catch {
    if (p.signal.aborted) return count;
  }

  // Process one post frame: fetch, open, verify, emit. Returns false only on a
  // transport failure fetching the body — the caller then drops the socket so
  // the reconnect replays from the un-advanced cursor (never skipping the post
  // by processing a later one first). Everything else advances.
  const processPost = async (frame: Extract<FeedFrame, { type: "post" }>): Promise<boolean> => {
    const { seq, slot_id: slotId } = frame;
    if (!slotId || seq <= lastSeq) return true;

    let raw: ArrayBuffer | null;
    try {
      raw = await ctx.client.getSlot(ctx.feed.feed_id, ctx.feedKey, slotId, { signal: p.signal });
    } catch {
      return false;
    }
    if (raw === null) {
      advance(seq); // genuine 404: deleted or aged out between index and fetch
      return true;
    }

    let header: FeedHeader;
    let body: ArrayBuffer;
    try {
      const opened = await cryptoClient.feedOpen({
        feedKeyHex: ctx.feedKeyHex,
        feedId: ctx.feed.feed_id,
        blob: raw,
      });
      header = opened.header;
      body = opened.body;
    } catch {
      advance(seq); // drop bad/foreign post
      return true;
    }

    if (header.smid === ctx.feed.member_id) {
      advance(seq); // our own broadcast comes back — don't re-download it
      return true;
    }

    // Resolve the sender from the roster. A post frame can beat its join frame
    // only across a reconnect, so if the sender is unknown, do a plain
    // (no-TOFU) roster fetch to name them. TOFU is advisory — delivery never
    // blocks on it.
    let found = roster.find((m) => m.member_id === header.smid);
    if (!found) {
      try {
        roster = await ctx.client.listMembers(ctx.feed.feed_id, ctx.feedKey, {
          signal: p.signal,
        });
        p.onRoster?.(roster);
        found = roster.find((m) => m.member_id === header.smid);
      } catch {
        if (p.signal.aborted) return true;
      }
    }
    let senderName = "(unknown)";
    if (found) {
      senderName = found.name || senderName;
      // Drop if a cached identity key changed (spoof).
      if (found.identity_pubkey && found.identity_pubkey !== header.sik) {
        advance(seq);
        return true;
      }
    }

    count++;
    p.onFile({ name: header.fn, body, bytes: body.byteLength, peerName: senderName, slot_id: slotId });
    advance(seq);
    return true;
  };

  const handleMember = async (frame: Extract<FeedFrame, { type: "member" }>): Promise<void> => {
    const m = frame.member;
    if (frame.action === "leave") {
      roster = roster.filter((x) => x.member_id !== m.member_id);
    } else {
      roster = [...roster.filter((x) => x.member_id !== m.member_id), m];
      await tofuMember(m, ctx, p.hooks, known);
    }
    p.onRoster?.(roster);
  };

  // Self-healing: the generator reconnects internally on transport drops, but a
  // throw from the consumer body (a callback) would otherwise escape and wedge
  // the watch for the rest of the session.
  while (!p.signal.aborted) {
    try {
      for await (const frame of ctx.client.watch(ctx.feed.feed_id, ctx.feedKey, {
        getSince: () => lastSeq,
        signal: p.signal,
      })) {
        if (frame.type === "post") {
          if (!(await processPost(frame))) break; // drop the socket; replay from lastSeq
        } else if (frame.type === "member") {
          await handleMember(frame);
        } else if (frame.type === "deleted") {
          p.onDeleted?.(frame.slot_id);
        }
      }
      if (p.signal.aborted) break;
    } catch {
      if (p.signal.aborted) break;
    }
    await sleep(WS_RECONNECT_MIN_MS, p.signal);
  }
  return count;
}
