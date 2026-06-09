// Bridges the framework-free feed orchestration to the store. Each action builds
// an AbortController, wires onProgress / TOFU / persistence callbacks to store
// actions, and translates results and errors into per-feed timeline rows.

import { useStore } from "../state/store";
import { runSend } from "../orchestration/send";
import { runReceive } from "../orchestration/receive";
import { openFeed, joinFeed, leaveFeed, type TofuHooks } from "../orchestration/feed-actions";
import { cryptoClient } from "../worker/cryptoClient";
import { friendlyRelayMessage } from "../relay/errors";
import type { Feed } from "../types";

function tofuHooks(): TofuHooks {
  return {
    isPinned: (sigPub) => useStore.getState().isPinned(sigPub),
    onTofu: (req) => useStore.getState().requestTofu(req),
    pinPeer: (sigPub, name) => useStore.getState().pinPeer(sigPub, name),
  };
}

function downloadBlob(name: string, body: ArrayBuffer): void {
  const url = URL.createObjectURL(new Blob([body], { type: "application/octet-stream" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

function newId(): string {
  return typeof crypto.randomUUID === "function"
    ? crypto.randomUUID()
    : `e${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
}

export function useTransfer() {
  const phase = useStore((s) => s.transfer.phase);
  const kind = useStore((s) => s.transfer.kind);
  const active = phase !== "idle" && phase !== "done" && phase !== "error";
  // `sending` blocks composer/rail actions only while an explicit user-driven
  // send is in flight. An ambient receive watch (started on feed select) sits
  // in "transferring" indefinitely and must NOT lock the UI.
  const sending = active && kind === "send";

  async function send(feed: Feed, payload: { name: string; data: ArrayBuffer }): Promise<void> {
    const s = useStore.getState();
    if (!s.identity) return;
    const id = newId();
    s.appendTimeline(feed.name, {
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
          useStore.getState().patchTimelineEntry(feed.name, id, { progress: f });
        },
        onRoster: (roster) =>
          useStore.getState().patchFeed(feed.name, { members_cache: roster }),
        signal: abort.signal,
      });
      useStore.getState().finishTransfer(result);
      useStore.getState().patchTimelineEntry(feed.name, id, {
        status: "served",
        progress: 1,
        recipients: result.recipients,
      });
    } catch (e) {
      if (abort.signal.aborted) {
        useStore.getState().patchTimelineEntry(feed.name, id, {
          status: "failed",
          error: "canceled",
        });
        return;
      }
      const msg = friendlyRelayMessage(e);
      useStore.getState().failTransfer(msg);
      useStore.getState().patchTimelineEntry(feed.name, id, { status: "failed", error: msg });
    }
  }

  /** Ambient feed watcher. Caller owns the AbortController (typically a
   * useEffect cleanup on selected-feed change). Deliberately does NOT touch
   * the transfer slice — that's reserved for explicit user-driven sends, so
   * the composer stays responsive while the loop runs. */
  async function receive(feed: Feed, signal: AbortSignal): Promise<void> {
    const s = useStore.getState();
    if (!s.identity) return;
    try {
      await runReceive({
        feed,
        identity: s.identity,
        hooks: tofuHooks(),
        onProgress: () => {
          // Intentionally no-op: the timeline rows carry per-file status; we
          // don't surface "watching feed" in the global transfer slice anymore.
        },
        onFile: (f) => {
          // Re-resolve the feed each time — auto_save may have flipped mid-watch.
          const live = useStore.getState().feeds.find((x) => x.name === feed.name);
          const autoSave = live?.auto_save ?? false;
          const id = newId();
          if (autoSave) {
            downloadBlob(f.name, f.body);
            useStore.getState().appendTimeline(feed.name, {
              id,
              kind: "receive",
              name: f.name,
              bytes: f.bytes,
              at: Date.now(),
              peerName: f.peerName,
              status: "saved",
            });
          } else {
            useStore.getState().appendTimeline(feed.name, {
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
        onAdvance: (ts) => useStore.getState().patchFeed(feed.name, { last_post_ts: ts }),
        onRoster: (roster) =>
          useStore.getState().patchFeed(feed.name, { members_cache: roster }),
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
  function saveHeld(feedName: string, id: string): void {
    const row = useStore.getState().timelines[feedName]?.find((r) => r.id === id);
    if (!row || row.status !== "held" || !row.body) return;
    downloadBlob(row.name, row.body);
    useStore.getState().patchTimelineEntry(feedName, id, {
      status: "saved",
      body: undefined,
    });
  }

  /** Drop a held received file from memory without writing it to disk. */
  function discardHeld(feedName: string, id: string): void {
    useStore.getState().patchTimelineEntry(feedName, id, {
      status: "discarded",
      body: undefined,
    });
  }

  /** Mint a new feed (needs a configured relay). Throws on failure. */
  async function open(name: string, ttlSeconds?: number): Promise<Feed> {
    const s = useStore.getState();
    if (!s.identity) throw new Error("no identity");
    if (!s.relay) throw new Error("no relay configured — set one in settings to open a feed.");
    const feed = await openFeed({ identity: s.identity, relay: s.relay, name, ttlSeconds });
    useStore.getState().upsertFeed(feed);
    return feed;
  }

  /** Join a feed by URL (no relay secret required). Throws on failure. */
  async function join(url: string, name: string): Promise<Feed> {
    const s = useStore.getState();
    if (!s.identity) throw new Error("no identity");
    const feed = await joinFeed({ identity: s.identity, url, name, hooks: tofuHooks() });
    useStore.getState().upsertFeed(feed);
    return feed;
  }

  async function leave(feed: Feed): Promise<void> {
    const s = useStore.getState();
    if (s.identity) await leaveFeed(feed, s.identity);
    useStore.getState().removeFeed(feed.name);
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
