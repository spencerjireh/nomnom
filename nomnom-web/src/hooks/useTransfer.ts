// Bridges the framework-free feed orchestration to the store. Each action builds
// an AbortController, wires onProgress / TOFU / persistence callbacks to store
// actions, and translates results and errors into channel timeline rows.

import { useStore } from "../state/store";
import { runSend } from "../orchestration/send";
import { runReceive } from "../orchestration/receive";
import { openFeed, joinFeed, leaveFeed, type TofuHooks } from "../orchestration/feed-actions";
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

export function useTransfer() {
  const phase = useStore((s) => s.transfer.phase);
  const kind = useStore((s) => s.transfer.kind);
  const active = phase !== "idle" && phase !== "done" && phase !== "error";
  // `sending` blocks composer actions only while an explicit user-driven send is
  // in flight. An ambient receive watch sits in "transferring" indefinitely and
  // must NOT lock the UI.
  const sending = active && kind === "send";

  /** Send one file to every other device on the channel. */
  async function send(payload: { name: string; data: ArrayBuffer }): Promise<void> {
    const s = useStore.getState();
    const feed = s.channel;
    if (!s.identity || !feed) return;
    const id = newId();
    s.appendTimeline({
      id,
      kind: "send",
      name: payload.name,
      bytes: payload.data.byteLength,
      at: Date.now(),
      status: "in_flight",
      progress: 0,
    });
    const abort = new AbortController();
    s.beginTransfer("send", abort);
    try {
      const result = await runSend({
        feed,
        identity: s.identity,
        payload,
        hooks: tofuHooks(),
        onProgress: (p, l, f) => {
          useStore.getState().updateProgress(p, l, f);
          useStore.getState().patchTimelineEntry(id, { progress: f });
        },
        onRoster: (roster) => useStore.getState().patchChannel({ members_cache: roster }),
        signal: abort.signal,
      });
      useStore.getState().finishTransfer(result);
      useStore.getState().patchTimelineEntry(id, {
        status: "served",
        progress: 1,
        recipients: result.recipients,
      });
    } catch (e) {
      if (abort.signal.aborted) {
        useStore.getState().patchTimelineEntry(id, { status: "failed", error: "canceled" });
        return;
      }
      const msg = friendlyRelayMessage(e);
      useStore.getState().failTransfer(msg);
      useStore.getState().patchTimelineEntry(id, { status: "failed", error: msg });
    }
  }

  /** Ambient channel watcher. Caller owns the AbortController (typically a
   * useEffect cleanup). Deliberately does NOT touch the transfer slice — that's
   * reserved for explicit user-driven sends, so the composer stays responsive
   * while the loop runs. */
  async function receive(feed: Feed, signal: AbortSignal): Promise<void> {
    const s = useStore.getState();
    if (!s.identity) return;
    // Identity is pinned for the watch's lifetime: it's this device's own
    // signing key, which only changes via factoryReset — and that aborts the
    // watch (via the signal), so a fresh receive() picks up the new identity.
    try {
      await runReceive({
        feed,
        identity: s.identity,
        hooks: tofuHooks(),
        onProgress: () => {
          // Intentionally no-op: timeline rows carry per-file status.
        },
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
      // The watch loop is best-effort — a transient network error shouldn't
      // wedge the UI. Log and let the next useEffect cycle restart it.
      console.warn("nomnom: receive watch failed:", friendlyRelayMessage(e));
    }
  }

  /** Save a held received file to disk and mark the timeline row as saved. */
  function saveHeld(id: string): void {
    const row = useStore.getState().timeline.find((r) => r.id === id);
    if (!row || row.status !== "held" || !row.body) return;
    downloadBlob(row.name, row.body);
    useStore.getState().patchTimelineEntry(id, { status: "saved", body: undefined });
  }

  /** Drop a held received file from memory without writing it to disk. */
  function discardHeld(id: string): void {
    useStore.getState().patchTimelineEntry(id, { status: "discarded", body: undefined });
  }

  /** Create the channel (owner only — needs a configured relay). Throws on failure. */
  async function open(): Promise<Feed> {
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
  async function join(secret: string): Promise<Feed> {
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
  async function leave(): Promise<void> {
    const s = useStore.getState();
    const feed = s.channel;
    if (!feed) return;
    if (s.identity) await leaveFeed(feed, s.identity);
    useStore.getState().leaveChannel();
  }

  function cancel(): void {
    const t = useStore.getState().transfer;
    t.abort?.abort();
    cryptoClient.cancel();
    useStore.getState().resolveTofu(false);
    useStore.getState().resetTransfer();
  }

  return { send, receive, saveHeld, discardHeld, open, join, leave, cancel, sending };
}
