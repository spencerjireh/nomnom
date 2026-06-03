import type { Identity } from "./crypto/identity";

export type { Identity };

export interface RelayConfig {
  url: string;
  secret: string;
}

/** A member of a feed, as carried in a member card / roster entry. */
export interface Member {
  member_id: string;
  identity_pubkey: string; // Ed25519 sig pubkey, hex
  name: string;
  joined_at?: number;
}

/** A feed this device has opened or joined. Shape mirrors the CLI's feeds.json. */
export interface Feed {
  name: string; // local nickname
  feed_id: string;
  feed_token: string;
  url: string; // https://host/f/<token>
  expires_at: number;
  joined_at: number;
  member_id: string; // this device's id within the feed
  members_cache: Member[];
  last_post_ts: number;
}

/**
 * A globally-pinned identity (TOFU), keyed by Ed25519 sig_pub. Mirrors the CLI's
 * known_peers.json v2 records (id `feed-<sig_pub16>`).
 */
export interface PeerPin {
  name: string;
  sig_pub: string;
  first_seen: number;
  nickname?: string;
}

export type PeerStore = Record<string, PeerPin>; // peerId -> pin

/** A first-contact prompt for a newly-seen feed member. */
export interface TofuRequest {
  peerName: string;
  sigPub: string;
  fingerprint: string;
}

export type OnTofu = (req: TofuRequest) => Promise<boolean>;

/** Coarse phase for the transfer panel; `label` carries the human string. */
export type Phase =
  | "idle"
  | "preparing"
  | "encrypting"
  | "transferring"
  | "decrypting"
  | "done"
  | "error";

export type OnProgress = (phase: Phase, label: string, fraction: number) => void;

export interface TransferResult {
  name?: string;
  bytes?: number;
  /** send: how many other members the post reaches. */
  recipients?: number;
  /** receive: sender display name + the downloaded file name. */
  peerName?: string;
  outName?: string;
}

/** A file pulled off a feed during a receive watch (already downloaded). */
export interface ReceivedItem {
  name: string;
  bytes: number;
  peerName: string;
  at: number; // epoch ms
}

/** The synthetic global pin id for an Ed25519 identity (matches CLI `_feed_peer_id`). */
export function feedPeerId(sigPubHex: string): string {
  return `feed-${sigPubHex.slice(0, 16)}`;
}
