// Single zustand store. Chosen over Context+reducers because worker progress
// ticks are high-frequency (selectors re-render only the panel that cares), the
// slices are read across components, and the orchestration layer runs OUTSIDE
// React and needs imperative get()/set().

import { create } from "zustand";
import { persistence } from "./persistence";
import { generateIdentity } from "../crypto/identity";
import { cryptoClient } from "../worker/cryptoClient";
import {
  feedPeerId,
  type Feed,
  type Identity,
  type PeerStore,
  type Phase,
  type RelayConfig,
  type TimelineEntry,
  type TofuRequest,
  type TransferResult,
} from "../types";

export type TransferKind = "send" | "receive";

interface TransferSlice {
  kind: TransferKind | null;
  phase: Phase;
  label: string;
  progress: number; // 0..1
  error: string | null;
  result: TransferResult | null;
  abort: AbortController | null;
}

const IDLE_TRANSFER: TransferSlice = {
  kind: null,
  phase: "idle",
  label: "",
  progress: 0,
  error: null,
  result: null,
  abort: null,
};

interface Store {
  identity: Identity | null;
  relay: RelayConfig | null;
  feeds: Feed[];
  selectedFeed: string | null;
  peers: PeerStore;
  transfer: TransferSlice;
  /** Per-feed session timeline. In-memory only — never persisted. */
  timelines: Record<string, TimelineEntry[]>;
  /** Per-feed "last viewed at" (epoch ms). Drives the activity dot on the rail. */
  viewedAt: Record<string, number>;
  tofu: { request: TofuRequest; resolve: (ok: boolean) => void } | null;

  hydrate: () => void;
  setName: (name: string) => void;
  setRelay: (cfg: RelayConfig) => void;

  // feeds
  upsertFeed: (feed: Feed) => void;
  removeFeed: (name: string) => void;
  renameFeed: (oldName: string, newName: string) => boolean;
  patchFeed: (name: string, patch: Partial<Feed>) => void;
  setFeedAutoSave: (name: string, autoSave: boolean) => void;
  selectFeed: (name: string | null) => void;

  // TOFU pins (keyed by Ed25519 sig_pub)
  isPinned: (sigPub: string) => boolean;
  pinPeer: (sigPub: string, name: string) => void;
  forgetPeer: (peerId: string) => void;
  renamePeer: (peerId: string, nickname: string) => void;

  beginTransfer: (kind: TransferKind, abort: AbortController) => void;
  updateProgress: (phase: Phase, label: string, progress: number) => void;
  finishTransfer: (result: TransferResult) => void;
  failTransfer: (message: string) => void;
  resetTransfer: () => void;

  // timeline
  appendTimeline: (feedName: string, entry: TimelineEntry) => void;
  patchTimelineEntry: (feedName: string, id: string, patch: Partial<TimelineEntry>) => void;
  markFeedViewed: (feedName: string) => void;

  requestTofu: (request: TofuRequest) => Promise<boolean>;
  resolveTofu: (ok: boolean) => void;

  factoryReset: () => void;
}

export const useStore = create<Store>((set, get) => {
  const persistFeeds = () => persistence.saveFeeds({ feeds: get().feeds });
  const persistSelected = () => persistence.saveLastSelectedFeed(get().selectedFeed);

  return {
    identity: null,
    relay: null,
    feeds: [],
    selectedFeed: null,
    peers: {},
    transfer: IDLE_TRANSFER,
    timelines: {},
    viewedAt: {},
    tofu: null,

    hydrate: () => {
      let identity = persistence.loadIdentity();
      if (!identity) {
        identity = generateIdentity();
        persistence.saveIdentity(identity);
      }
      const { feeds } = persistence.loadFeeds();
      const lastSelected = persistence.loadLastSelectedFeed();
      const selectedFeed = lastSelected && feeds.some((f) => f.name === lastSelected)
        ? lastSelected
        : null;
      set({
        identity,
        relay: persistence.loadRelay(),
        feeds,
        selectedFeed,
        peers: persistence.loadPeers(),
      });
    },

    setName: (name) => {
      const id = get().identity;
      if (!id) return;
      const next = { ...id, name };
      persistence.saveIdentity(next);
      set({ identity: next });
    },

    setRelay: (cfg) => {
      persistence.saveRelay(cfg);
      set({ relay: cfg });
    },

    // --- feeds ---

    upsertFeed: (feed) => {
      const feeds = get().feeds.filter((f) => f.name !== feed.name);
      feeds.push(feed);
      set({ feeds });
      persistFeeds();
    },

    removeFeed: (name) => {
      const feeds = get().feeds.filter((f) => f.name !== name);
      const { timelines, viewedAt } = get();
      const { [name]: _t, ...timelinesRest } = timelines;
      const { [name]: _v, ...viewedAtRest } = viewedAt;
      let selectedFeed = get().selectedFeed;
      if (selectedFeed === name) selectedFeed = null;
      set({ feeds, selectedFeed, timelines: timelinesRest, viewedAt: viewedAtRest });
      persistFeeds();
      persistSelected();
    },

    renameFeed: (oldName, newName) => {
      const feeds = get().feeds;
      if (feeds.some((f) => f.name === newName)) return false;
      const next = feeds.map((f) => (f.name === oldName ? { ...f, name: newName } : f));
      const { timelines, viewedAt, selectedFeed } = get();
      const nextTimelines = { ...timelines };
      if (oldName in nextTimelines) {
        nextTimelines[newName] = nextTimelines[oldName];
        delete nextTimelines[oldName];
      }
      const nextViewedAt = { ...viewedAt };
      if (oldName in nextViewedAt) {
        nextViewedAt[newName] = nextViewedAt[oldName];
        delete nextViewedAt[oldName];
      }
      const nextSelected = selectedFeed === oldName ? newName : selectedFeed;
      set({ feeds: next, timelines: nextTimelines, viewedAt: nextViewedAt, selectedFeed: nextSelected });
      persistFeeds();
      if (nextSelected !== selectedFeed) persistSelected();
      return true;
    },

    patchFeed: (name, patch) => {
      const next = get().feeds.map((f) => (f.name === name ? { ...f, ...patch } : f));
      set({ feeds: next });
      persistFeeds();
    },

    setFeedAutoSave: (name, autoSave) => {
      get().patchFeed(name, { auto_save: autoSave });
    },

    selectFeed: (name) => {
      if (get().selectedFeed === name) return;
      set({ selectedFeed: name });
      persistSelected();
    },

    // --- TOFU pins ---

    isPinned: (sigPub) => get().peers[feedPeerId(sigPub)]?.sig_pub === sigPub,

    pinPeer: (sigPub, name) => {
      const peers = { ...get().peers };
      const existing = Object.entries(peers).find(([, p]) => p.sig_pub === sigPub);
      if (existing) {
        const [id, rec] = existing;
        peers[id] = { ...rec, name };
      } else {
        peers[feedPeerId(sigPub)] = {
          name,
          sig_pub: sigPub,
          first_seen: Math.floor(Date.now() / 1000),
        };
      }
      persistence.savePeers(peers);
      set({ peers });
    },

    forgetPeer: (peerId) => {
      const peers = { ...get().peers };
      delete peers[peerId];
      persistence.savePeers(peers);
      set({ peers });
    },

    renamePeer: (peerId, nickname) => {
      const peers = { ...get().peers };
      const rec = peers[peerId];
      if (!rec) return;
      peers[peerId] = { ...rec, nickname };
      persistence.savePeers(peers);
      set({ peers });
    },

    // --- transfer ---

    beginTransfer: (kind, abort) =>
      set({ transfer: { ...IDLE_TRANSFER, kind, phase: "preparing", abort } }),

    updateProgress: (phase, label, progress) =>
      set((s) => ({ transfer: { ...s.transfer, phase, label, progress } })),

    finishTransfer: (result) =>
      set((s) => ({
        transfer: { ...s.transfer, phase: "done", label: "done", progress: 1, result, abort: null },
      })),

    failTransfer: (message) =>
      set((s) => ({ transfer: { ...s.transfer, phase: "error", error: message, abort: null } })),

    resetTransfer: () => set({ transfer: IDLE_TRANSFER }),

    // --- timeline ---

    appendTimeline: (feedName, entry) =>
      set((s) => {
        const rows = s.timelines[feedName] ?? [];
        return { timelines: { ...s.timelines, [feedName]: [entry, ...rows] } };
      }),

    patchTimelineEntry: (feedName, id, patch) =>
      set((s) => {
        const rows = s.timelines[feedName];
        if (!rows) return {};
        return {
          timelines: {
            ...s.timelines,
            [feedName]: rows.map((r) => (r.id === id ? { ...r, ...patch } : r)),
          },
        };
      }),

    markFeedViewed: (feedName) =>
      set((s) => ({ viewedAt: { ...s.viewedAt, [feedName]: Date.now() } })),

    requestTofu: (request) =>
      new Promise<boolean>((resolve) => {
        set({
          tofu: {
            request,
            resolve: (ok) => {
              set({ tofu: null });
              resolve(ok);
            },
          },
        });
      }),

    resolveTofu: (ok) => {
      const t = get().tofu;
      if (t) t.resolve(ok);
    },

    factoryReset: () => {
      // Tear down any in-flight transfer first so its abort controller and
      // worker thread don't leak past the reset.
      get().transfer.abort?.abort();
      cryptoClient.cancel();
      persistence.reset();
      const identity = generateIdentity();
      persistence.saveIdentity(identity);
      set({
        identity,
        relay: null,
        feeds: [],
        selectedFeed: null,
        peers: {},
        transfer: IDLE_TRANSFER,
        timelines: {},
        viewedAt: {},
        tofu: null,
      });
    },
  };
});
