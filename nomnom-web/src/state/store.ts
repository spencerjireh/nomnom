// Single zustand store. Chosen over Context+reducers because worker progress
// ticks are high-frequency (selectors re-render only the panel that cares), the
// slices are read across components, and the orchestration layer runs OUTSIDE
// React and needs imperative get()/set().
//
// nomnom has exactly one "channel" — a single permanent feed shared across the
// user's own devices — so the store holds a `channel` singleton and one flat
// session `timeline`, not a list of feeds.

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
  channel: Feed | null;
  peers: PeerStore;
  transfer: TransferSlice;
  /** The channel's session timeline. In-memory only — never persisted. */
  timeline: TimelineEntry[];
  tofu: { request: TofuRequest; resolve: (ok: boolean) => void } | null;

  hydrate: () => void;
  setName: (name: string) => void;
  setRelay: (cfg: RelayConfig) => void;

  // channel
  setChannel: (feed: Feed) => void;
  patchChannel: (patch: Partial<Feed>) => void;
  setAutoSave: (autoSave: boolean) => void;
  leaveChannel: () => void;

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
  appendTimeline: (entry: TimelineEntry) => void;
  patchTimelineEntry: (id: string, patch: Partial<TimelineEntry>) => void;

  requestTofu: (request: TofuRequest) => Promise<boolean>;
  resolveTofu: (ok: boolean) => void;

  factoryReset: () => void;
}

export const useStore = create<Store>((set, get) => {
  const persistChannel = () => persistence.saveChannel(get().channel);

  return {
    identity: null,
    relay: null,
    channel: null,
    peers: {},
    transfer: IDLE_TRANSFER,
    timeline: [],
    tofu: null,

    hydrate: () => {
      let identity = persistence.loadIdentity();
      if (!identity) {
        identity = generateIdentity();
        persistence.saveIdentity(identity);
      }
      set({
        identity,
        relay: persistence.loadRelay(),
        channel: persistence.loadChannel(),
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

    // --- channel ---

    setChannel: (feed) => {
      // Re-pairing to a different channel clears the previous timeline.
      const replacing = get().channel?.feed_id !== feed.feed_id;
      set({ channel: feed, timeline: replacing ? [] : get().timeline });
      persistChannel();
    },

    patchChannel: (patch) => {
      const cur = get().channel;
      if (!cur) return;
      set({ channel: { ...cur, ...patch } });
      persistChannel();
    },

    setAutoSave: (autoSave) => get().patchChannel({ auto_save: autoSave }),

    leaveChannel: () => {
      // Resolve any open first-contact prompt so an awaiting roster refresh
      // doesn't hang forever once its channel is gone.
      get().tofu?.resolve(false);
      set({ channel: null, timeline: [] });
      persistChannel();
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

    appendTimeline: (entry) =>
      set((s) => ({ timeline: [entry, ...s.timeline] })),

    patchTimelineEntry: (id, patch) =>
      set((s) => ({
        timeline: s.timeline.map((r) => (r.id === id ? { ...r, ...patch } : r)),
      })),

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
      // Resolve a pending TOFU prompt so a receive-side roster refresh awaiting
      // it doesn't dangle (the transfer slice doesn't cover receive-side TOFU).
      get().tofu?.resolve(false);
      cryptoClient.cancel();
      persistence.reset();
      const identity = generateIdentity();
      persistence.saveIdentity(identity);
      set({
        identity,
        relay: null,
        channel: null,
        peers: {},
        transfer: IDLE_TRANSFER,
        timeline: [],
        tofu: null,
      });
    },
  };
});
