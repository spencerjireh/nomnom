import type { Identity } from "./crypto/dh";

export type { Identity };

/** A pinned peer (TOFU). Shape mirrors the CLI's known_peers.json records. */
export interface PeerPin {
  name: string;
  ik_pub: string;
  first_seen: number;
  last_transfer?: number;
  transfer_count?: number;
  nickname?: string;
}

export type PeerStore = Record<string, PeerPin>; // device_id -> pin

export interface RelayConfig {
  url: string;
  secret: string;
}

export type TofuDecision = "match" | "new" | "changed";

export interface TofuRequest {
  decision: "new" | "changed";
  peerId: string;
  peerName: string;
  oldIk: string | null;
  newIk: string;
  fingerprint: string;
}

export type OnTofu = (req: TofuRequest) => Promise<boolean>;

/** Called when a transfer/pair commits a peer, so the caller updates the store. */
export interface PinUpdate {
  decision: TofuDecision;
  peerId: string;
  peerName: string;
  peerIk: string;
}
export type OnPin = (update: PinUpdate) => void;

/** Coarse phase for the transfer panel; `label` carries the human string. */
export type Phase =
  | "idle"
  | "handshaking"
  | "encrypting"
  | "transferring"
  | "decrypting"
  | "done"
  | "error";

export type OnProgress = (phase: Phase, label: string, fraction: number) => void;

export interface TransferResult {
  name?: string;
  bytes?: number;
  peerName?: string;
  /** For receive: the decrypted file is delivered as a download. */
  outName?: string;
}

export interface PairResult {
  peerId: string;
  peerName: string;
  peerIk: string;
  role: "initiator" | "responder";
}
