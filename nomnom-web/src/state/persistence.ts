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
  channel: "nomnom:channel",
  // Legacy multi-feed keys; read once for migration, cleared on reset.
  feeds: "nomnom:feeds",
  lastSelectedFeed: "nomnom:lastSelectedFeed",
  peers: "nomnom:peers",
  schema: "nomnom:schema",
} as const;

// Schema 2 = feeds v2 (Ed25519 identity). A schema-1 (legacy DH) identity is
// incompatible and gets discarded on hydrate. Adding per-feed `auto_save` is
// not a schema bump — missing fields are filled with safe defaults on load.
export const SCHEMA = 2;

function withAutoSave(raw: Omit<Feed, "auto_save"> & { auto_save?: boolean }): Feed {
  return { ...(raw as Feed), auto_save: raw.auto_save === true };
}

function read<T>(key: string): T | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as T) : null;
  } catch (e) {
    // Corrupt JSON means the identity/channel silently vanishes — leave a trace.
    console.warn(`nomnom: ignoring unreadable ${key} in localStorage`, e);
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

/** Tolerant feed shape check: requires the wire fields, leaves `auto_save`
 * optional so legacy feeds (saved before the toggle existed) load cleanly. */
function isFeed(f: unknown): f is Omit<Feed, "auto_save"> & { auto_save?: boolean } {
  if (!f || typeof f !== "object") return false;
  const x = f as Feed;
  return (
    typeof x.name === "string" &&
    typeof x.feed_id === "string" &&
    typeof x.feed_token === "string" &&
    typeof x.url === "string" &&
    typeof x.member_id === "string" &&
    typeof x.expires_at === "number" &&
    typeof x.joined_at === "number" &&
    typeof x.last_post_ts === "number"
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

  // The single channel = one stored Feed. New installs use `nomnom:channel`;
  // older multi-feed installs are migrated by taking the first valid feed out
  // of the legacy `nomnom:feeds` array.
  loadChannel: (): Feed | null => {
    const direct = read<unknown>(K.channel);
    if (isFeed(direct)) return withAutoSave(direct);
    const legacy = read<{ feeds?: unknown }>(K.feeds);
    if (legacy && Array.isArray(legacy.feeds)) {
      for (const raw of legacy.feeds) {
        if (isFeed(raw)) return withAutoSave(raw);
      }
    }
    return null;
  },
  saveChannel: (feed: Feed | null): void => {
    if (feed === null) {
      try {
        localStorage.removeItem(K.channel);
      } catch {
        // ignore
      }
    } else {
      write(K.channel, feed);
    }
  },

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
