// Bridges the framework-free feed orchestration to the store. Module-level
// (not a hook): every function reads state imperatively via useStore.getState(),
// so each has a stable identity that components and effects can depend on
// directly. Each action builds an AbortController, wires TOFU / persistence
// callbacks to store actions, and translates results and errors into channel
// timeline rows.

import { useStore } from "./store";
import { runSend } from "../orchestration/send";
import { runReceive } from "../orchestration/receive";
import { runHistory } from "../orchestration/history";
import { openFeed, joinFeed, leaveFeed, feedContext, type TofuHooks } from "../orchestration/feed-actions";
import { cryptoClient } from "../worker/cryptoClient";
import { friendlyRelayMessage } from "../relay/errors";
import { CHANNEL_NAME, PERMANENT_TTL_SECONDS } from "../config";
import { downloadBlob } from "../util/dom";
import { newId } from "../util/ids";
import type { Feed } from "../types";

function tofuHooks(): TofuHooks {
  return {
    isPinned: (sigPub) => useStore.getState().isPinned(sigPub),
    onTofu: (req) => useStore.getState().requestTofu(req),
    pinPeer: (sigPub, name) => useStore.getState().pinPeer(sigPub, name),
  };
}

/** Send one payload to every other device on the channel. The bytes are read
 * lazily inside the try so a failed read (e.g. a staged file deleted from disk
 * before send) surfaces as a failed timeline row like any other send error.
 * Never throws — all outcomes land on the row. */
export async function send(payload: {
  name: string;
  size: number;
  read: () => Promise<ArrayBuffer>;
}): Promise<void> {
  const s = useStore.getState();
  const feed = s.channel;
  if (!s.identity || !feed) return;
  const id = newId();
  s.appendTimeline({
    id,
    kind: "send",
    name: payload.name,
    bytes: payload.size,
    at: Date.now(),
    status: "in_flight",
    progress: 0,
  });
  const abort = new AbortController();
  s.beginSend(abort);
  try {
    const data = await payload.read();
    const result = await runSend({
      feed,
      identity: s.identity,
      payload: { name: payload.name, data },
      hooks: tofuHooks(),
      onProgress: (f) => useStore.getState().patchTimelineEntry(id, { progress: f }),
      onRoster: (roster) => useStore.getState().patchChannel({ members_cache: roster }),
      signal: abort.signal,
    });
    useStore.getState().patchTimelineEntry(id, {
      status: "served",
      progress: 1,
      recipients: result.recipients,
    });
  } catch (e) {
    const msg = abort.signal.aborted ? "canceled" : friendlyRelayMessage(e);
    useStore.getState().patchTimelineEntry(id, { status: "failed", error: msg });
  } finally {
    useStore.getState().endSend();
  }
}

/** Ambient channel watcher. Caller owns the AbortController (typically a
 * useEffect cleanup). Deliberately does NOT touch the transfer slice — that's
 * reserved for explicit user-driven sends, so the composer stays responsive
 * while the loop runs. */
export async function receive(feed: Feed, signal: AbortSignal): Promise<void> {
  const s = useStore.getState();
  if (!s.identity) return;
  const identity = s.identity;

  // Phase 1: rebuild the session timeline from the relay's still-live posts, so a
  // refresh restores history (sent + received) instead of starting blank. Runs
  // once per mount; the live watch below resumes after the newest rebuilt post.
  let resumeFrom = feed.last_post_ts;
  try {
    const { rows, maxCursor } = await runHistory({ feed, identity, signal });
    if (signal.aborted) return; // StrictMode unmount / re-pair — don't clobber
    useStore.getState().rebuildTimeline(rows);
    resumeFrom = Math.max(feed.last_post_ts, maxCursor);
    useStore.getState().patchChannel({ last_post_ts: resumeFrom });
  } catch (e) {
    if (signal.aborted) return;
    // runHistory is best-effort and already swallows relay errors; a throw here
    // is unexpected. Surface it but still fall through to the live watch.
    console.warn("nomnom: history rebuild failed:", friendlyRelayMessage(e));
  }

  // Phase 2: live watch. Identity is pinned for the watch's lifetime: it's this
  // device's own signing key, which only changes via factoryReset — and that
  // aborts the watch (via the signal), so a fresh receive() picks up the new
  // identity.
  try {
    await runReceive({
      feed: { ...feed, last_post_ts: resumeFrom },
      identity,
      hooks: tofuHooks(),
      onFile: (f) => {
        // Re-resolve auto_save each time — it may have flipped mid-watch.
        const autoSave = useStore.getState().channel?.auto_save ?? false;
        const id = newId();
        if (autoSave) {
          downloadBlob(f.name, f.body);
          useStore.getState().appendTimeline({
            id,
            kind: "receive",
            name: f.name,
            bytes: f.bytes,
            at: Date.now(),
            peerName: f.peerName,
            status: "saved",
            body: f.body,
          });
        } else {
          useStore.getState().appendTimeline({
            id,
            kind: "receive",
            name: f.name,
            bytes: f.bytes,
            at: Date.now(),
            peerName: f.peerName,
            status: "held",
            body: f.body,
          });
        }
      },
      onAdvance: (ts) => useStore.getState().patchChannel({ last_post_ts: ts }),
      onRoster: (roster) => useStore.getState().patchChannel({ members_cache: roster }),
      signal,
    });
  } catch (e) {
    if (signal.aborted) return;
    // The watch loops self-heal on transient errors, so a rejection here means
    // a programmer error — surface it without wedging the UI.
    console.warn("nomnom: receive watch failed:", friendlyRelayMessage(e));
  }
}

/** Save a received file to disk. Live-received rows keep their body in memory,
 * so save is instant. History-rebuilt rows carry only a slot_id (the rebuild
 * doesn't hold every retained file in memory at once); those fetch + decrypt the
 * slot on demand, download it, and attach the body so view / copy / re-save then
 * work without another round-trip. Surfaces a row error if the slot has expired
 * off the relay. Never throws. */
export async function saveHeld(id: string): Promise<void> {
  const s = useStore.getState();
  const row = s.timeline.find((r) => r.id === id);
  if (!row) return;

  // Body already in memory (live receipt, or a previously-saved rebuilt row).
  if (row.body) {
    downloadBlob(row.name, row.body);
    useStore.getState().patchTimelineEntry(id, { status: "saved", error: undefined });
    return;
  }

  // Rebuilt row — lazily fetch + decrypt by slot_id.
  const feed = s.channel;
  if (!row.slot_id || !s.identity || !feed) return;
  try {
    const ctx = feedContext(feed, s.identity);
    const raw = await ctx.client.getSlot(feed.feed_id, ctx.feedKey, row.slot_id);
    if (raw === null) throw new Error("this file is no longer on the relay (expired)");
    const opened = await cryptoClient.feedOpen({
      feedKeyHex: ctx.feedKeyHex,
      feedId: feed.feed_id,
      blob: raw,
    });
    // The user may have discarded the row during the fetch/decrypt — don't
    // download or patch a row that's gone (or was replaced).
    const current = useStore.getState().timeline.find((r) => r.id === id);
    if (!current || current.slot_id !== row.slot_id) return;
    downloadBlob(current.name, opened.body);
    useStore.getState().patchTimelineEntry(id, {
      status: "saved",
      body: opened.body,
      error: undefined,
    });
  } catch (e) {
    useStore.getState().patchTimelineEntry(id, { error: friendlyRelayMessage(e) });
  }
}

/** Discard a received file: remove its row (and bytes) from the timeline. */
export function discardHeld(id: string): void {
  useStore.getState().removeTimelineEntry(id);
}

/** Create the channel (owner only — needs a configured relay). Throws on failure. */
export async function openChannel(): Promise<Feed> {
  const s = useStore.getState();
  if (!s.identity) throw new Error("no identity");
  if (!s.relay) throw new Error("no relay configured — set one in settings to create a channel.");
  const feed = await openFeed({
    identity: s.identity,
    relay: s.relay,
    name: CHANNEL_NAME,
    ttlSeconds: PERMANENT_TTL_SECONDS,
  });
  useStore.getState().setChannel(feed);
  return feed;
}

/** Add this device by pasting the channel secret (no relay secret required). */
export async function joinChannel(secret: string): Promise<Feed> {
  const s = useStore.getState();
  if (!s.identity) throw new Error("no identity");
  const feed = await joinFeed({
    identity: s.identity,
    url: secret,
    name: CHANNEL_NAME,
    hooks: tofuHooks(),
  });
  useStore.getState().setChannel(feed);
  return feed;
}

/** Leave the channel on this device. */
export async function leaveChannel(): Promise<void> {
  const s = useStore.getState();
  const feed = s.channel;
  if (!feed) return;
  if (s.identity) await leaveFeed(feed, s.identity);
  useStore.getState().leaveChannel();
}
