// SEND = broadcast one file into a feed. Refresh the roster (TOFU on new
// members), seal the post, PUT it to a random slot. Mirrors nomnom.py cmd_send.

import { cryptoClient } from "../worker/cryptoClient";
import { feedContext, refreshRoster, type TofuHooks } from "./feed-actions";
import { randomToken } from "../util/ids";
import { MAX_PAYLOAD_BYTES } from "../config";
import type { Feed, Identity, Member, OnProgress, TransferResult } from "../types";

export interface SendParams {
  feed: Feed;
  identity: Identity;
  payload: { name: string; data: ArrayBuffer };
  hooks: TofuHooks;
  onProgress: OnProgress;
  onRoster?: (roster: Member[]) => void;
  signal: AbortSignal;
}

export async function runSend(p: SendParams): Promise<TransferResult> {
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

  p.onProgress("preparing", "refreshing roster", 0);
  const roster = await refreshRoster(ctx, p.hooks, p.signal);
  p.onRoster?.(roster);
  const others = roster.filter((m) => m.member_id !== p.feed.member_id);

  p.onProgress("encrypting", "encrypting", 0);
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
    (_phase, fraction) => p.onProgress("encrypting", "encrypting", fraction),
  );

  p.onProgress("transferring", "broadcasting", 0.95);
  await ctx.client.putSlot(ctx.feed.feed_id, ctx.feedKey, randomToken(12), new Uint8Array(blob), p.signal);
  p.onProgress("done", "done", 1);
  return { name: p.payload.name, bytes: byteLength, recipients: others.length };
}
