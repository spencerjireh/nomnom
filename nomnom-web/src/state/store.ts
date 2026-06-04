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
  type ReceivedItem,
  type RelayConfig,
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
  defaultFeed: string | null;
  peers: PeerStore;
  transfer: TransferSlice;
  received: ReceivedItem[];
  tofu: { request: TofuRequest; resolve: (ok: boolean) => void } | null;

  hydrate: () => void;
  setName: (name: string) => void;
  setRelay: (cfg: RelayConfig) => void;

  // feeds
  upsertFeed: (feed: Feed, makeDefault?: boolean) => void;
  removeFeed: (name: string) => void;
  setDefaultFeed: (name: string | null) => void;
  renameFeed: (oldName: string, newName: string) => boolean;
  patchFeed: (name: string, patch: Partial<Feed>) => void;

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
  pushReceived: (item: ReceivedItem) => void;
  clearReceived: () => void;

  requestTofu: (request: TofuRequest) => Promise<boolean>;
  resolveTofu: (ok: boolean) => void;

  factoryReset: () => void;
}

export const useStore = create<Store>((set, get) => {
  const persistFeeds = () =>
    persistence.saveFeeds({ default: get().defaultFeed, feeds: get().feeds });

  return {
    identity: null,
    relay: null,
    feeds: [],
    defaultFeed: null,
    peers: {},
    transfer: IDLE_TRANSFER,
    received: [],
    tofu: null,

    hydrate: () => {
      let identity = persistence.loadIdentity();
      if (!identity) {
        identity = generateIdentity();
        persistence.saveIdentity(identity);
      }
      const feeds = persistence.loadFeeds();
      set({
        identity,
        relay: persistence.loadRelay(),
        feeds: feeds.feeds,
        defaultFeed: feeds.default,
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

    upsertFeed: (feed, makeDefault = false) => {
      const feeds = get().feeds.filter((f) => f.name !== feed.name);
      feeds.push(feed);
      // First feed becomes the default automatically (mirrors _add_or_replace_feed).
      let defaultFeed = get().defaultFeed;
      if (makeDefault || (!defaultFeed && feeds.length === 1)) defaultFeed = feed.name;
      set({ feeds, defaultFeed });
      persistFeeds();
    },

    removeFeed: (name) => {
      const feeds = get().feeds.filter((f) => f.name !== name);
      let defaultFeed = get().defaultFeed;
      if (defaultFeed === name) defaultFeed = feeds[0]?.name ?? null;
      set({ feeds, defaultFeed });
      persistFeeds();
    },

    setDefaultFeed: (name) => {
      if (name !== null && !get().feeds.some((f) => f.name === name)) return;
      set({ defaultFeed: name });
      persistFeeds();
    },

    renameFeed: (oldName, newName) => {
      const feeds = get().feeds;
      if (feeds.some((f) => f.name === newName)) return false;
      const next = feeds.map((f) => (f.name === oldName ? { ...f, name: newName } : f));
      const defaultFeed = get().defaultFeed === oldName ? newName : get().defaultFeed;
      set({ feeds: next, defaultFeed });
      persistFeeds();
      return true;
    },

    patchFeed: (name, patch) => {
      const next = get().feeds.map((f) => (f.name === name ? { ...f, ...patch } : f));
      set({ feeds: next });
      persistFeeds();
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

    pushReceived: (item) => set((s) => ({ received: [item, ...s.received] })),
    clearReceived: () => set({ received: [] }),

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
        defaultFeed: null,
        peers: {},
        transfer: IDLE_TRANSFER,
        received: [],
        tofu: null,
      });
    },
  };
});
