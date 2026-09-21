// HISTORY = rebuild the channel timeline from the relay on load. The relay keeps
// each channel's encrypted posts (slots) for ~30 days (not delete-on-read), so a
// page refresh can re-fetch, decrypt, and reconstruct the session timeline —
// sent AND received — instead of starting blank. Nothing is persisted locally;
// bodies are decrypted in memory only.
//
// This is a one-shot sweep, distinct from the live watch (runReceive): it lists
// every slot since seq 0 (the relay only serves posts inside its 30-day window,
// so there is no client-side age filter), and unlike the live watch it INCLUDES
// our own posts (as `send` rows) — after a refresh the original in-memory send
// rows are gone, so they must be rebuilt too.

import { cryptoClient } from "../worker/cryptoClient";
import { feedContext, type FeedContext } from "./feed-actions";
import { newId } from "../util/ids";
import { mapLimit } from "../util/concurrency";
import type { FeedHeader } from "../crypto/feeds";
import type { Feed, Identity, Member, TimelineEntry } from "../types";

// Fetch + decrypt this many slots at once during the rebuild. Overlaps the relay
// round-trips (the decrypts still serialize behind the single crypto worker) and
// bounds peak memory: only ~this-many decrypted bodies are alive at once, since
// rebuilt receive rows keep just a slot_id and re-fetch their body lazily.
const HISTORY_FETCH_CONCURRENCY = 6;

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
  /** seq to resume the live watch from — the newest slot that existed at sweep
   * time, so runReceive won't re-emit anything we just rebuilt. */
  maxCursor: number;
}

/** Rebuild the timeline from the relay's retained posts. Best-effort: any relay
 * or decode failure yields the rows gathered so far (or none) — the live watch
 * still takes over, and the next refresh re-sweeps from scratch. */
export async function runHistory(p: HistoryParams): Promise<HistoryResult> {
  const ctx = p.ctx ?? feedContext(p.feed, p.identity);

  // Resolve sender names from the roster. Plain listMembers (no TOFU) — first-
  // contact prompts are owned by the live watch's roster loop, so the rebuild
  // never blocks delivery on a modal (mirrors receive.ts). Best-effort: fall
  // back to the cached roster if the relay is unreachable.
  let roster: Member[] = ctx.feed.members_cache ?? [];
  try {
    roster = await ctx.client.listMembers(ctx.feed.feed_id, ctx.feedKey, { signal: p.signal });
  } catch {
    if (p.signal.aborted) return { rows: [], maxCursor: p.feed.last_seq };
    // keep the cached roster
  }

  let slots;
  try {
    slots = await ctx.client.listSlots(ctx.feed.feed_id, ctx.feedKey, {
      since: 0,
      signal: p.signal,
    });
  } catch {
    return { rows: [], maxCursor: p.feed.last_seq };
  }

  // Resume the live watch after the newest slot that existed now — computed from
  // the list, not per-slot success, so a post that fails to fetch/decode this
  // round is simply absent until the next refresh (never a duplicate). The
  // relay lists ascending by seq, so the newest is last.
  const maxCursor = slots.length ? Math.max(p.feed.last_seq, slots[slots.length - 1].seq) : p.feed.last_seq;

  // Fetch + decrypt with bounded concurrency (overlaps network waits) instead of
  // strictly serially. mapLimit preserves slot order, so the oldest-first input
  // yields oldest-first rows; a slot that fails to fetch/decode becomes null and
  // is compacted out (never a duplicate, mirroring the serial version).
  const built = await mapLimit(
    slots,
    HISTORY_FETCH_CONCURRENCY,
    async (slot): Promise<TimelineEntry | null> => {
      if (p.signal.aborted) return null;

      let raw: ArrayBuffer | null;
      try {
        raw = await ctx.client.getSlot(ctx.feed.feed_id, ctx.feedKey, slot.slot_id, { signal: p.signal });
      } catch {
        return null; // transient fetch failure — skip; next refresh retries
      }
      if (raw === null) return null; // slot gone / already cleaned up

      let header: FeedHeader;
      try {
        // Read only the header; the decrypted body is intentionally dropped so a
        // refresh never holds every retained file in memory at once. Received
        // rows carry a slot_id and re-fetch their body lazily on save.
        const opened = await cryptoClient.feedOpen({
          feedKeyHex: ctx.feedKeyHex,
          feedId: ctx.feed.feed_id,
          blob: raw,
        });
        header = opened.header;
      } catch {
        return null; // foreign / tampered / undecryptable post
      }

      const at = (header.pa || slot.created_at) * 1000;

      if (header.smid === ctx.feed.member_id) {
        // Our own post — reconstruct as a delivered send row (no body needed).
        return {
          id: newId(),
          kind: "send",
          name: header.fn,
          bytes: header.fs,
          at,
          status: "served",
          slot_id: slot.slot_id,
        };
      }

      // A received post. Resolve the sender and reject a changed identity key
      // (spoof), mirroring the live watch.
      const found = roster.find((m) => m.member_id === header.smid);
      if (found && found.identity_pubkey && found.identity_pubkey !== header.sik) {
        return null;
      }
      return {
        id: newId(),
        kind: "receive",
        name: header.fn,
        bytes: header.fs,
        at,
        status: "held", // rebuild never auto-downloads; auto_save gates live receipt only
        peerName: found?.name || "(unknown)",
        slot_id: slot.slot_id, // body fetched lazily on save
      };
    },
  );

  const rows = built.filter((r): r is TimelineEntry => r !== null);
  rows.reverse(); // newest-first
  return { rows, maxCursor };
}
