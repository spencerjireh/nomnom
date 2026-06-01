// Trust-on-first-use gate. Mirrors nomnom.py _tofu_decision / _relay_run_tofu.

import { ikFingerprint } from "../crypto/fingerprint";
import type { OnTofu, PeerStore, TofuDecision } from "../types";

export function tofuDecision(pins: PeerStore, peerId: string, peerIk: string): TofuDecision {
  const pinned = pins[peerId]?.ik_pub;
  if (pinned == null) return "new";
  return pinned === peerIk ? "match" : "changed";
}

/**
 * Returns true if the transfer may proceed. "match" is automatic; "new"/"changed"
 * prompt the user (via onTofu) with the peer fingerprint.
 */
export async function runTofu(
  decision: TofuDecision,
  peerId: string,
  peerName: string,
  peerIk: string,
  oldIk: string | null,
  onTofu: OnTofu,
): Promise<boolean> {
  if (decision === "match") return true;
  return onTofu({
    decision,
    peerId,
    peerName,
    oldIk,
    newIk: peerIk,
    fingerprint: ikFingerprint(peerIk),
  });
}
