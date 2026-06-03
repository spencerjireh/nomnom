import { ikFingerprint } from "../crypto/fingerprint";

/** Renders an identity-key fingerprint (Ed25519 sig_pub) in the mustard accent. */
export function Fingerprint({ hex }: { hex: string }) {
  return <span className="fingerprint">{ikFingerprint(hex)}</span>;
}
