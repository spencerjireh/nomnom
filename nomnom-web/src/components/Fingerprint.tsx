import { ikFingerprint } from "../crypto/fingerprint";

/** Renders an identity-key fingerprint in the mustard accent, monospace. */
export function Fingerprint({ ikHex }: { ikHex: string }) {
  return <span className="fingerprint">{ikFingerprint(ikHex)}</span>;
}
