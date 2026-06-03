// localStorage persistence. The wire/crypto interop with the CLI is fixed by the
// feed protocol; these local shapes simply mirror the CLI's identity.json /
// feeds.json / known_peers.json for readability. NOTE (accepted tradeoff): the
// Ed25519 seed (sig_priv) and the relay secret live here in plaintext, readable
// by any script on this origin — the same trust model as the CLI's
// ~/.config/nomnom files. The app ships a strict CSP and no third-party scripts.
// File payloads are NEVER persisted (they stay in memory during a transfer).

import type { Feed, Identity, PeerStore, RelayConfig } from "../types";

const K = {
  identity: "nomnom:identity",
  relay: "nomnom:relay",
  feeds: "nomnom:feeds",
  peers: "nomnom:peers",
  schema: "nomnom:schema",
} as const;

// Schema 2 = feeds v2 (Ed25519 identity). A schema-1 (legacy DH) identity is
// incompatible and gets discarded on hydrate.
export const SCHEMA = 2;

export interface FeedsConfig {
  default: string | null;
  feeds: Feed[];
}

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
    // Quota/availability failures are non-fatal — the in-memory store still
    // works for the session; only persistence across reloads is lost.
  }
}

/** True if the stored identity is a usable feeds-v2 (Ed25519) identity. */
function isFeedIdentity(id: unknown): id is Identity {
  return (
    !!id &&
    typeof id === "object" &&
    typeof (id as Identity).sig_priv === "string" &&
    typeof (id as Identity).sig_pub === "string"
  );
}

export const persistence = {
  loadIdentity: (): Identity | null => {
    if (read<number>(K.schema) !== SCHEMA) return null; // legacy / absent
    const id = read<unknown>(K.identity);
    return isFeedIdentity(id) ? id : null;
  },
  saveIdentity: (id: Identity): void => {
    write(K.identity, id);
    write(K.schema, SCHEMA);
  },

  loadRelay: (): RelayConfig | null => read<RelayConfig>(K.relay),
  saveRelay: (cfg: RelayConfig): void => write(K.relay, cfg),

  loadFeeds: (): FeedsConfig => {
    const cfg = read<FeedsConfig>(K.feeds);
    if (!cfg || !Array.isArray(cfg.feeds)) return { default: null, feeds: [] };
    const def = typeof cfg.default === "string" && cfg.feeds.some((f) => f.name === cfg.default)
      ? cfg.default
      : null;
    return { default: def, feeds: cfg.feeds };
  },
  saveFeeds: (cfg: FeedsConfig): void => write(K.feeds, cfg),

  loadPeers: (): PeerStore => read<PeerStore>(K.peers) ?? {},
  savePeers: (peers: PeerStore): void => write(K.peers, peers),

  reset: (): void => {
    for (const key of Object.values(K)) {
      try {
        localStorage.removeItem(key);
      } catch {
        // ignore
      }
    }
  },
};
