// RECEIVE = watch a feed for new posts. Discover new slots over the SSE /stream
// endpoint (real push from a Durable Object); for each, GET it, feed_open
// (verifies signature + content hash), skip our own posts, then hand the file to
// onFile. A separate loop keeps the roster fresh (TOFU). Runs until the signal
// aborts. Falls back to the /slots long-poll if the relay has no /stream.
// Mirrors nomnom.py cmd_receive.

import { cryptoClient } from "../worker/cryptoClient";
import { feedContext, refreshRoster, type TofuHooks } from "./feed-actions";
import { RELAY_WAIT_MS } from "../config";
import { StreamUnsupportedError } from "../relay/errors";
import type { FeedHeader } from "../crypto/feeds";
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

  const refresh = async () => {
    roster = await refreshRoster(
      { ...ctx, feed: { ...ctx.feed, members_cache: roster } },
      p.hooks,
      p.signal,
    );
    p.onRoster?.(roster);
  };

  // Process one slot notification: fetch, open, verify, emit. Shared by the SSE
  // and long-poll discovery paths. Idempotent: slots at/under the cursor (a
  // backlog/live overlap on reconnect) are skipped.
  const processSlot = async (slotId: string, createdAt: number): Promise<void> => {
    if (!slotId || createdAt <= lastTs) return;

    let raw: ArrayBuffer | null;
    try {
      raw = await ctx.client.getSlot(ctx.feed.feed_id, ctx.feedKey, slotId, { signal: p.signal });
    } catch {
      if (p.signal.aborted) return;
      advance(createdAt);
      return;
    }
    if (raw === null) {
      advance(createdAt);
      return;
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
      advance(createdAt); // drop bad/foreign post
      return;
    }

    if (header.smid === ctx.feed.member_id) {
      advance(createdAt); // our own broadcast comes back — don't re-download it
      return;
    }

    // Resolve the sender from the roster. A post can beat the roster loop, so if
    // the sender is unknown, do a plain (no-TOFU) roster fetch to name them.
    // TOFU is advisory and runs on the roster loop — delivery never blocks on it
    // (and a prompt here would race the roster loop's single modal).
    let found = roster.find((m) => m.member_id === header.smid);
    if (!found) {
      try {
        roster = await ctx.client.listMembers(ctx.feed.feed_id, ctx.feedKey, {
          signal: p.signal,
        });
        p.onRoster?.(roster);
        found = roster.find((m) => m.member_id === header.smid);
      } catch {
        if (p.signal.aborted) return;
      }
    }
    let senderName = "(unknown)";
    if (found) {
      senderName = found.name || senderName;
      // Drop if a cached identity key changed (spoof).
      if (found.identity_pubkey && found.identity_pubkey !== header.sik) {
        advance(createdAt);
        return;
      }
    }

    count++;
    p.onFile({ name: header.fn, body, bytes: body.byteLength, peerName: senderName });
    advance(createdAt);
  };

  // Keep the roster fresh for TOFU + sender names, decoupled from slot discovery.
  const rosterLoop = async () => {
    while (!p.signal.aborted) {
      try {
        await refresh();
      } catch {
        if (p.signal.aborted) break;
        // non-fatal — try again next tick
      }
      await sleep(RELAY_WAIT_MS, p.signal);
    }
  };

  // Long-poll fallback used when the relay has no /stream endpoint.
  const longPollLoop = async () => {
    while (!p.signal.aborted) {
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
        await processSlot(entry.slot_id, entry.created_at ?? 0);
      }
    }
  };

  const slotLoop = async () => {
    p.onProgress("transferring", "watching feed", 0);
    try {
      for await (const entry of ctx.client.streamSlotEvents(
        ctx.feed.feed_id,
        ctx.feedKey,
        { getSince: () => lastTs, signal: p.signal },
      )) {
        await processSlot(entry.slot_id, entry.created_at ?? 0);
      }
    } catch (e) {
      if (p.signal.aborted) return;
      if (!(e instanceof StreamUnsupportedError)) throw e;
      // Relay predates /stream — long-poll for the rest of the session.
      await longPollLoop();
    }
  };

  await Promise.all([rosterLoop(), slotLoop()]);
  return count;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(resolve, ms);
    signal.addEventListener(
      "abort",
      () => {
        clearTimeout(t);
        resolve();
      },
      { once: true },
    );
  });
}
