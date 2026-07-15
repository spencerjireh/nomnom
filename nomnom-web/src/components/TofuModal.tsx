import { useStore } from "../state/store";

/** Trust-on-first-use prompt for a newly-seen feed member. Driven by store.tofu
 * (set by the orchestration's onTofu callback). Identities are pinned globally by
 * Ed25519 sig_pub, so there is no "key changed" case — a different key is simply a
 * different identity. Mirrors the CLI's _tofu_check_feed_member. */
export function TofuModal() {
  const tofu = useStore((s) => s.tofu);
  const resolveTofu = useStore((s) => s.resolveTofu);
  if (!tofu) return null;
  const { request } = tofu;

  return (
    <div className="modal-backdrop" role="dialog" aria-modal="true">
      <section className="ticket modal">
        <div className="ticket-head">
          <span>** NEW DEVICE **</span>
        </div>
        <p>
          first contact with <strong>{request.peerName}</strong> on your channel.
        </p>
        <p className="kv">
          <span className="dim">fingerprint</span>{" "}
          <span className="fingerprint">{request.fingerprint}</span>
        </p>
        <p className="dim small">verify this out-of-band with them if it matters.</p>
        <div className="modal-actions">
          {/* Pass the rendered identity so a stale double-click can't settle the
              next queued prompt with this decision. */}
          <button type="button" className="btn ghost" onClick={() => resolveTofu(false, request.sigPub)}>
            not now
          </button>
          <button type="button" className="btn primary" onClick={() => resolveTofu(true, request.sigPub)}>
            trust &amp; pin
          </button>
        </div>
      </section>
    </div>
  );
}
