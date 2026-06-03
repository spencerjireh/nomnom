// RECEIVE = watch a feed for new posts. Long-poll /slots?since=<last_post_ts> in
// a loop; for each new slot, GET it, feed_open (verifies signature + content
// hash), skip our own posts, then hand the file to onFile. Runs until the signal
// aborts. Mirrors nomnom.py cmd_receive.

import { cryptoClient } from "../worker/cryptoClient";
import { feedContext, refreshRoster, type TofuHooks } from "./feed-actions";
import { RELAY_WAIT_MS } from "../config";
import type { Feed, Identity, Member, OnProgress } from "../types";

export interface ReceivedFile {
  name: string;
  body: ArrayBuffer;
  bytes: number;
  peerName: string;
}

export interface ReceiveParams {
  feed: Feed;
  identity: Identity;
  hooks: TofuHooks;
  onProgress: OnProgress;
  onFile: (f: ReceivedFile) => void;
  /** Persist forward progress (last_post_ts). */
  onAdvance: (lastPostTs: number) => void;
  onRoster?: (roster: Member[]) => void;
  signal: AbortSignal;
}

/** Watch until aborted; returns the number of files received. */
export async function runReceive(p: ReceiveParams): Promise<number> {
  const ctx = feedContext(p.feed, p.identity);
  let lastTs = p.feed.last_post_ts;
  let roster: Member[] = p.feed.members_cache ?? [];
  let count = 0;

  const advance = (createdAt: number) => {
    if (createdAt > lastTs) {
      lastTs = createdAt;
      p.onAdvance(lastTs);
    }
  };

  while (!p.signal.aborted) {
    // Roster refresh + TOFU before each slot long-poll. Up to a 30s lag between a
    // join and the prompt, but it keeps everything on one logical flow.
    try {
      roster = await refreshRoster(
        { ...ctx, feed: { ...ctx.feed, members_cache: roster } },
        p.hooks,
        p.signal,
      );
      p.onRoster?.(roster);
    } catch {
      if (p.signal.aborted) break;
      // non-fatal — fall through to the slot poll
    }

    p.onProgress("transferring", count ? `watching · ${count} received` : "watching feed", 0);
    let slots;
    try {
      slots = await ctx.client.listSlots(ctx.feed.feed_id, ctx.feedKey, {
        sinceTs: lastTs,
        waitMs: RELAY_WAIT_MS,
        signal: p.signal,
      });
    } catch (e) {
      if (p.signal.aborted) break;
      throw e;
    }

    for (const entry of slots) {
      if (p.signal.aborted) break;
      const slotId = entry.slot_id;
      const createdAt = entry.created_at || 0;
      if (!slotId) continue;

      let raw: ArrayBuffer | null;
      try {
        raw = await ctx.client.getSlot(ctx.feed.feed_id, ctx.feedKey, slotId, { signal: p.signal });
      } catch {
        if (p.signal.aborted) break;
        advance(createdAt);
        continue;
      }
      if (raw === null) {
        advance(createdAt);
        continue;
      }

      let header, body: ArrayBuffer;
      try {
        const opened = await cryptoClient.feedOpen({
          feedKeyHex: ctx.feedKeyHex,
          feedId: ctx.feed.feed_id,
          blob: raw,
        });
        header = opened.header;
        body = opened.body;
      } catch {
        advance(createdAt); // drop bad/foreign post
        continue;
      }

      if (header.smid === ctx.feed.member_id) {
        advance(createdAt); // our own broadcast comes back — don't re-download it
        continue;
      }

      // Resolve sender name from the roster; drop if a cached identity key changed.
      let senderName = "(unknown)";
      let spoofed = false;
      for (const m of roster) {
        if (m.member_id === header.smid) {
          senderName = m.name || senderName;
          if (m.identity_pubkey && m.identity_pubkey !== header.sik) spoofed = true;
          break;
        }
      }
      if (spoofed) {
        advance(createdAt);
        continue;
      }

      count++;
      p.onFile({ name: header.fn, body, bytes: body.byteLength, peerName: senderName });
      advance(createdAt);
    }
  }
  return count;
}
