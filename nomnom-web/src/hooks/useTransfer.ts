// Bridges the framework-free feed orchestration to the store. Each action builds
// an AbortController, wires onProgress / TOFU / persistence callbacks to store
// actions, and translates results and errors into the transfer slice.

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

export function useTransfer() {
  const phase = useStore((s) => s.transfer.phase);
  const busy = phase !== "idle" && phase !== "done" && phase !== "error";

  async function send(feed: Feed, payload: { name: string; data: ArrayBuffer }): Promise<void> {
    const s = useStore.getState();
    if (!s.identity) return;
    const abort = new AbortController();
    s.beginTransfer("send", abort);
    try {
      const result = await runSend({
        feed,
        identity: s.identity,
        payload,
        hooks: tofuHooks(),
        onProgress: (p, l, f) => useStore.getState().updateProgress(p, l, f),
        onRoster: (roster) => useStore.getState().patchFeed(feed.name, { members_cache: roster }),
        signal: abort.signal,
      });
      useStore.getState().finishTransfer(result);
    } catch (e) {
      if (abort.signal.aborted) return;
      useStore.getState().failTransfer(friendlyRelayMessage(e));
    }
  }

  async function receive(feed: Feed): Promise<void> {
    const s = useStore.getState();
    if (!s.identity) return;
    const abort = new AbortController();
    s.beginTransfer("receive", abort);
    s.clearReceived();
    try {
      await runReceive({
        feed,
        identity: s.identity,
        hooks: tofuHooks(),
        onProgress: (p, l, f) => useStore.getState().updateProgress(p, l, f),
        onFile: (f) => {
          downloadBlob(f.name, f.body);
          useStore.getState().pushReceived({
            name: f.name,
            bytes: f.bytes,
            peerName: f.peerName,
            at: Date.now(),
          });
        },
        onAdvance: (ts) => useStore.getState().patchFeed(feed.name, { last_post_ts: ts }),
        onRoster: (roster) => useStore.getState().patchFeed(feed.name, { members_cache: roster }),
        signal: abort.signal,
      });
      // Loop ended (signal aborted) — fall back to idle.
      useStore.getState().resetTransfer();
    } catch (e) {
      if (abort.signal.aborted) {
        useStore.getState().resetTransfer();
        return;
      }
      useStore.getState().failTransfer(friendlyRelayMessage(e));
    }
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

  return { send, receive, open, join, leave, cancel, busy };
}
