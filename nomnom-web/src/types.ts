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

/** A feed this device has opened or joined. Shape mirrors the CLI's feeds.json,
 * plus a local-only `auto_save` toggle: when true, received files decrypt
 * straight to disk; when false (the safe default) they hold in the timeline
 * awaiting an explicit [save] / [discard]. */
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
  auto_save: boolean;
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

/** Progress callback: overall fraction in [0, 1]. Rendered on timeline rows. */
export type OnProgress = (fraction: number) => void;

/** A row in a feed's session timeline — either a file we sent or a file we
 * received. `body` stays attached across held → saved so view/copy/re-save
 * keep working after a download; discarding removes the whole row (and with
 * it the bytes). */
export type TimelineKind = "send" | "receive";
export type TimelineStatus =
  | "in_flight"   // send: still encrypting/uploading
  | "served"      // send: delivered
  | "saved"       // receive: written to Downloads (or already-trusted auto-save)
  | "held"        // receive: decrypted, waiting for the user to save or discard
  | "failed";     // send: error

export interface TimelineEntry {
  id: string;
  kind: TimelineKind;
  name: string;
  bytes: number;
  at: number; // epoch ms
  status: TimelineStatus;
  peerName?: string;   // receive only
  recipients?: number; // send only
  progress?: number;   // 0..1, for in_flight sends
  error?: string;      // failed sends
  body?: ArrayBuffer;  // receive only; present from "held" through "saved"
  slot_id?: string;    // receive only; set on history-rebuilt rows so their body
                       // can be re-fetched + decrypted lazily on save (the
                       // rebuild doesn't keep every body in memory at once)
}

/** The synthetic global pin id for an Ed25519 identity (matches CLI `_feed_peer_id`). */
export function feedPeerId(sigPubHex: string): string {
  return `feed-${sigPubHex.slice(0, 16)}`;
}
