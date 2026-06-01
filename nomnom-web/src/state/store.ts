// Single zustand store. Chosen over Context+reducers because worker progress ticks
// are high-frequency (selectors re-render only the panel that cares), the four
// slices are read across components, and the orchestration layer runs OUTSIDE
// React and needs imperative get()/set().

import { create } from "zustand";
import { persistence } from "./persistence";
import { generateIdentity } from "../crypto/dh";
import type {
  Identity,
  RelayConfig,
  PeerStore,
  PeerPin,
  TransferResult,
  TofuRequest,
  PinUpdate,
  Phase,
} from "../types";

interface TransferSlice {
  kind: "send" | "receive" | "pair" | null;
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
  peers: PeerStore;
  transfer: TransferSlice;
  tofu: { request: TofuRequest; resolve: (ok: boolean) => void } | null;

  hydrate: () => void;
  setName: (name: string) => void;
  setRelay: (cfg: RelayConfig) => void;

  applyPin: (update: PinUpdate) => void;
  forgetPin: (peerId: string) => void;
  renamePin: (peerId: string, name: string) => void;

  beginTransfer: (kind: "send" | "receive" | "pair", abort: AbortController) => void;
  updateProgress: (phase: Phase, label: string, progress: number) => void;
  finishTransfer: (result: TransferResult) => void;
  failTransfer: (message: string) => void;
  resetTransfer: () => void;

  requestTofu: (request: TofuRequest) => Promise<boolean>;
  resolveTofu: (ok: boolean) => void;

  factoryReset: () => void;
}

export const useStore = create<Store>((set, get) => ({
  identity: null,
  relay: null,
  peers: {},
  transfer: IDLE_TRANSFER,
  tofu: null,

  hydrate: () => {
    let identity = persistence.loadIdentity();
    if (!identity) {
      identity = generateIdentity();
      persistence.saveIdentity(identity);
    }
    set({ identity, relay: persistence.loadRelay(), peers: persistence.loadPeers() });
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

  applyPin: ({ decision, peerId, peerName, peerIk }) => {
    // Mirrors nomnom.py _relay_pin_peer: on new/changed save name+ik; on match
    // leave name/ik untouched (a sender can't rename a pinned peer) — only bump
    // the per-transfer counters.
    const peers = { ...get().peers };
    const existing = peers[peerId];
    const now = Math.floor(Date.now() / 1000);
    let rec: PeerPin;
    if (decision === "new" || !existing) {
      rec = {
        name: peerName,
        ik_pub: peerIk,
        first_seen: existing?.first_seen ?? now,
        nickname: existing?.nickname,
      };
    } else if (decision === "changed") {
      rec = { ...existing, name: peerName, ik_pub: peerIk };
    } else {
      rec = { ...existing };
    }
    rec.last_transfer = now;
    rec.transfer_count = (existing?.transfer_count ?? 0) + 1;
    peers[peerId] = rec;
    persistence.savePeers(peers);
    set({ peers });
  },

  forgetPin: (peerId) => {
    const peers = { ...get().peers };
    delete peers[peerId];
    persistence.savePeers(peers);
    set({ peers });
  },

  renamePin: (peerId, name) => {
    const peers = { ...get().peers };
    const rec = peers[peerId];
    if (!rec) return;
    peers[peerId] = { ...rec, nickname: name };
    persistence.savePeers(peers);
    set({ peers });
  },

  beginTransfer: (kind, abort) =>
    set({ transfer: { ...IDLE_TRANSFER, kind, phase: "handshaking", abort } }),

  updateProgress: (phase, label, progress) =>
    set((s) => ({ transfer: { ...s.transfer, phase, label, progress } })),

  finishTransfer: (result) =>
    set((s) => ({
      transfer: { ...s.transfer, phase: "done", label: "done", progress: 1, result, abort: null },
    })),

  failTransfer: (message) =>
    set((s) => ({ transfer: { ...s.transfer, phase: "error", error: message, abort: null } })),

  resetTransfer: () => set({ transfer: IDLE_TRANSFER }),

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
    persistence.reset();
    const identity = generateIdentity();
    persistence.saveIdentity(identity);
    set({ identity, relay: null, peers: {}, transfer: IDLE_TRANSFER, tofu: null });
  },
}));
