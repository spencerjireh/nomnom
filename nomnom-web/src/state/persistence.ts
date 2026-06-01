// localStorage persistence. Shapes mirror the CLI's identity.json / known_peers.json
// so slot ids and fingerprints interoperate. NOTE (accepted tradeoff): ik_priv and
// the relay secret live here in plaintext, readable by any script on this origin —
// the same trust model as the CLI's ~/.config/nomnom files. The app ships a strict
// CSP and no third-party scripts to keep the origin script-clean. File payloads are
// NEVER persisted (they stay in memory during a transfer), so quota is a non-issue.

import type { Identity, PeerStore, RelayConfig } from "../types";

const K = {
  identity: "nomnom:identity",
  relay: "nomnom:relay",
  peers: "nomnom:peers",
  schema: "nomnom:schema",
} as const;

const SCHEMA = 1;

function read<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch {
    return null;
  }
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    // Quota/availability failures are non-fatal — the in-memory store still works
    // for the session; only persistence across reloads is lost.
  }
}

export const persistence = {
  loadIdentity: (): Identity | null => read<Identity>(K.identity),
  saveIdentity: (id: Identity): void => {
    write(K.identity, id);
    write(K.schema, SCHEMA);
  },

  loadRelay: (): RelayConfig | null => read<RelayConfig>(K.relay),
  saveRelay: (cfg: RelayConfig): void => write(K.relay, cfg),

  loadPeers: (): PeerStore => read<PeerStore>(K.peers) ?? {},
  savePeers: (peers: PeerStore): void => write(K.peers, peers),

  reset: (): void => {
    for (const key of [K.identity, K.relay, K.peers, K.schema]) {
      try {
        localStorage.removeItem(key);
      } catch {
        // ignore
      }
    }
  },
};
