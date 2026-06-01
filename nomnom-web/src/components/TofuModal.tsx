import { useStore } from "../state/store";
import { ikFingerprint } from "../crypto/fingerprint";

/** Trust-on-first-use prompt. Driven by store.tofu (set by the orchestration's
 * onTofu callback). Shows the peer fingerprint so the user can verify out-of-band
 * before pinning. Mirrors the CLI's _tofu_confirm_new / _tofu_confirm_change. */
export function TofuModal() {
  const tofu = useStore((s) => s.tofu);
  const resolveTofu = useStore((s) => s.resolveTofu);
  if (!tofu) return null;
  const { request } = tofu;
  const changed = request.decision === "changed";

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="ticket modal">
        <div className="ticket-head">
          <span>{changed ? "!! KEY CHANGED !!" : "** NEW GUEST **"}</span>
        </div>
        <p>
          {changed ? "the identity key for " : "first contact with "}
          <strong>{request.peerName}</strong>
          {changed ? " CHANGED." : ` (device ${request.peerId}).`}
        </p>
        {changed && (
          <p className="dim">
            Expected if it was reinstalled or wiped — but this is also what a
            man-in-the-middle looks like.
          </p>
        )}
        {changed && request.oldIk && (
          <p className="kv">
            <span className="dim">pinned</span>{" "}
            <span className="fingerprint strike">{ikFingerprint(request.oldIk)}</span>
          </p>
        )}
        <p className="kv">
          <span className="dim">{changed ? "offered" : "fingerprint"}</span>{" "}
          <span className="fingerprint">{request.fingerprint}</span>
        </p>
        <p className="dim small">verify this out-of-band with the sender if it matters.</p>
        <div className="modal-actions">
          <button type="button" className="btn ghost" onClick={() => resolveTofu(false)}>
            reject
          </button>
          <button type="button" className="btn primary" onClick={() => resolveTofu(true)}>
            {changed ? "trust new key" : "trust & pin"}
          </button>
        </div>
      </section>
    </div>
  );
}
