// HISTORY = rebuild the channel timeline from the relay on load. The relay keeps
// each channel's encrypted posts (slots) for ~30 days (not delete-on-read), so a
// page refresh can re-fetch, decrypt, and reconstruct the session timeline —
// sent AND received — instead of starting blank. Nothing is persisted locally;
// bodies are decrypted in memory only.
//
// This is a one-shot sweep, distinct from the live watch (runReceive): it lists
// every slot since the beginning (bounded by HISTORY_MAX_AGE_DAYS), and unlike
// the live watch it INCLUDES our own posts (as `send` rows) — after a refresh the
// original in-memory send rows are gone, so they must be rebuilt too.

import { cryptoClient } from "../worker/cryptoClient";
import { feedContext, type FeedContext } from "./feed-actions";
import { HISTORY_MAX_AGE_DAYS } from "../config";
import { newId } from "../util/ids";
import type { FeedHeader } from "../crypto/feeds";
import type { Feed, Identity, Member, TimelineEntry } from "../types";

export interface HistoryParams {
  feed: Feed;
  identity: Identity;
  signal: AbortSignal;
  /** Test seam: pre-built context (key + client). Defaults to feedContext(feed, identity). */
  ctx?: FeedContext;
}

export interface HistoryResult {
  /** Newest-first, matching the store's timeline convention. */
  rows: TimelineEntry[];
  /** created_at to resume the live watch from — the newest slot that existed at
   * sweep time, so runReceive won't re-emit anything we just rebuilt. */
  maxCursor: number;
}

/** Rebuild the timeline from the relay's retained posts. Best-effort: any relay
 * or decode failure yields the rows gathered so far (or none) — the live watch
 * still takes over, and the next refresh re-sweeps from scratch. */
export async function runHistory(p: HistoryParams): Promise<HistoryResult> {
  const ctx = p.ctx ?? feedContext(p.feed, p.identity);
  const floor = Math.floor(Date.now() / 1000) - HISTORY_MAX_AGE_DAYS * 86_400;

  // Resolve sender names from the roster. Plain listMembers (no TOFU) — first-
  // contact prompts are owned by the live watch's roster loop, so the rebuild
  // never blocks delivery on a modal (mirrors receive.ts). Best-effort: fall
  // back to the cached roster if the relay is unreachable.
  let roster: Member[] = ctx.feed.members_cache ?? [];
  try {
    roster = await ctx.client.listMembers(ctx.feed.feed_id, ctx.feedKey, { signal: p.signal });
  } catch {
    if (p.signal.aborted) return { rows: [], maxCursor: p.feed.last_post_ts };
    // keep the cached roster
  }

  let slots;
  try {
    slots = await ctx.client.listSlots(ctx.feed.feed_id, ctx.feedKey, {
      sinceTs: 0,
      waitMs: 0,
      signal: p.signal,
    });
  } catch {
    return { rows: [], maxCursor: p.feed.last_post_ts };
  }

  // Resume the live watch after the newest slot that existed now — computed from
  // the list, not per-slot success, so a post that fails to fetch/decode this
  // round is simply absent until the next refresh (never a duplicate).
  let maxCursor = p.feed.last_post_ts;
  for (const s of slots) maxCursor = Math.max(maxCursor, s.created_at ?? 0);

  // Oldest-first so the reversed result is newest-first.
  slots.sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0));

  const rows: TimelineEntry[] = [];
  for (const slot of slots) {
    if (p.signal.aborted) break;
    if ((slot.created_at ?? 0) < floor) continue; // older than the relay keeps

    let raw: ArrayBuffer | null;
    try {
      raw = await ctx.client.getSlot(ctx.feed.feed_id, ctx.feedKey, slot.slot_id, { signal: p.signal });
    } catch {
      continue; // transient fetch failure — skip; next refresh retries
    }
    if (raw === null) continue; // slot gone / already cleaned up

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
      continue; // foreign / tampered / undecryptable post
    }

    const at = (header.pa ? header.pa * 1000 : (slot.created_at ?? 0) * 1000);

    if (header.smid === ctx.feed.member_id) {
      // Our own post — reconstruct as a delivered send row (no body needed).
      rows.push({
        id: newId(),
        kind: "send",
        name: header.fn,
        bytes: header.fs,
        at,
        status: "served",
      });
      continue;
    }

    // A received post. Resolve the sender and reject a changed identity key
    // (spoof), mirroring the live watch.
    const found = roster.find((m) => m.member_id === header.smid);
    if (found && found.identity_pubkey && found.identity_pubkey !== header.sik) {
      continue;
    }
    rows.push({
      id: newId(),
      kind: "receive",
      name: header.fn,
      bytes: header.fs,
      at,
      status: "held", // rebuild never auto-downloads; auto_save gates live receipt only
      peerName: found?.name || "(unknown)",
      body,
    });
  }

  rows.reverse(); // newest-first
  return { rows, maxCursor };
}
