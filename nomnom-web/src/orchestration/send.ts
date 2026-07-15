// SEND = broadcast one file into a feed. Refresh the roster (TOFU on new
// members), seal the post, PUT it to a random slot. Mirrors nomnom.py cmd_send.

import { cryptoClient } from "../worker/cryptoClient";
import { feedContext, refreshRoster, type TofuHooks } from "./feed-actions";
import { randomToken } from "../util/ids";
import { MAX_PAYLOAD_BYTES } from "../config";
import type { Feed, Identity, Member, OnProgress } from "../types";

export interface SendParams {
  feed: Feed;
  identity: Identity;
  payload: { name: string; data: ArrayBuffer };
  hooks: TofuHooks;
  onProgress: OnProgress;
  onRoster?: (roster: Member[]) => void;
  signal: AbortSignal;
}

/** Returns the number of other members the post reaches. */
export async function runSend(p: SendParams): Promise<{ recipients: number }> {
  const byteLength = p.payload.data.byteLength; // capture before the buffer transfers
  if (byteLength === 0) {
    // A transferred/detached ArrayBuffer reads as length 0 on the main thread
    // (so does a genuinely empty file); the relay rejects an empty body either
    // way, so fail fast with a clear message instead of broadcasting nothing.
    throw new Error("payload is empty or its buffer was already consumed; re-read the file");
  }
  if (byteLength > MAX_PAYLOAD_BYTES) {
    throw new Error(`payload too large for relay (${byteLength} bytes; limit ${MAX_PAYLOAD_BYTES})`);
  }
  const ctx = feedContext(p.feed, p.identity);

  p.onProgress(0);
  const roster = await refreshRoster(ctx, p.hooks, p.signal);
  p.onRoster?.(roster);
  const others = roster.filter((m) => m.member_id !== p.feed.member_id);

  // Progress is split into two monotonic sub-ranges: the seal (XOR) pass fills
  // 0..0.9, the upload fills 0.9..1. The seal's own fraction ends at 1.0, so
  // forwarding it verbatim (then dropping back for the upload) made the bar jump
  // backward mid-send — scale it into its sub-range instead.
  const SEAL_FRACTION = 0.9;
  const blob = await cryptoClient.feedSeal(
    {
      feedKeyHex: ctx.feedKeyHex,
      feedId: ctx.feed.feed_id,
      senderMemberId: ctx.feed.member_id,
      senderSigPrivHex: p.identity.sig_priv,
      senderSigPubHex: p.identity.sig_pub,
      filename: p.payload.name,
      data: p.payload.data,
    },
    (_phase, fraction) => p.onProgress(fraction * SEAL_FRACTION),
  );

  p.onProgress(SEAL_FRACTION);
  await ctx.client.putSlot(ctx.feed.feed_id, ctx.feedKey, randomToken(12), new Uint8Array(blob), p.signal);
  p.onProgress(1);
  return { recipients: others.length };
}
